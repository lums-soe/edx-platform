import json
import logging
import re

from cms.djangoapps.contentstore.utils import get_lms_link_for_item, is_currently_visible_to_students
from courseware.courses import get_course_by_id
from django.conf import settings
from django.contrib.auth.models import User
from edx_ace import ace
from edx_ace.recipient import Recipient
from lms.djangoapps.discussion.tasks import _get_thread_url
from lms.lib.comment_client.comment import Comment
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey, UsageKey
from openassessment.fileupload import backends
from openassessment.fileupload.exceptions import FileUploadInternalError
from openedx.core.djangoapps.ace_common.template_context import get_base_template_context
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.djangoapps.site_configuration.models import SiteConfiguration
from openedx.core.djangoapps.theming.helpers import get_current_site
from openedx.features.edly.message_types import ContactUsSupportNotification
from openedx.features.edly.tasks import send_bulk_mail_to_students
from static_replace import replace_static_urls
from student.models import CourseEnrollment, user_by_anonymous_id
from submissions.api import _get_submission_model
from submissions.models import Submission
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.exceptions import ItemNotFoundError

log = logging.getLogger(__name__)
ENABLE_FORUM_NOTIFICATIONS_FOR_SITE_KEY = 'enable_forum_notifications'
COURSE_OUTLINE_CATEGORIES = ['vertical', 'sequential', 'chapter']


def notify_students_about_xblock_changes(xblock, publish, old_content):
    """
    This function is responsible for calling the related function by checking the xblock category.

    Arguments:
        xblock: Block of courses which has been published.
        publish: Param to check if xblock is going to be published.
        old_content: Old data of xblock before updating.
    """
    if (publish == 'make_public' and xblock.category in COURSE_OUTLINE_CATEGORIES
            and is_currently_visible_to_students(xblock)):
        _handle_section_publish(xblock)
    elif xblock.category == 'course_info' and xblock.location.block_id == 'handouts':
        _handle_handout_changes(xblock, old_content)


def get_email_params(xblock):
    """
    Generates the email params for any changes in course

    Arguments:
        xblock: xblock which is modified/created

    Returns:
        Dict containing the data for email.
    """
    email_params = {}
    site_id = ''
    course = get_course_by_id(xblock.location.course_key)
    site = get_current_site()
    if site:
        site_id = site.id
    email_params['site_id'] = site_id
    email_params['contact_mailing_address'] = settings.CONTACT_MAILING_ADDRESS
    email_params['course_url'] = _get_course_url(xblock.location.course_key)
    email_params['course_name'] = course.display_name_with_default
    email_params['display_name'] = xblock.display_name
    email_params['platform_name'] = settings.PLATFORM_NAME
    email_params['site_name'] = configuration_helpers.get_value(
        'SITE_NAME',
        settings.SITE_NAME
    )
    return email_params


def _get_course_url(course_key):
    return '{}/courses/{}'.format(settings.LMS_ROOT_URL, course_key)


def _handle_section_publish(xblock):
    """
    This function will send email to the enrolled students in the case
    of any outline changes like section, subsection, unit publish.

    Arguments:
        xblock: xblock which is modified/created
    """
    email_params = get_email_params(xblock)
    students = get_course_enrollments(xblock.location.course_key)
    email_params['change_url'] = get_xblock_lms_link(xblock.location)
    if xblock.category == 'vertical':
        email_params['change_type'] = 'Unit'
    elif xblock.category == 'sequential':
        email_params['change_type'] = 'Sub Section'
    else:
        email_params['change_type'] = 'Section'

    send_bulk_mail_to_students.delay(students, email_params, 'outline_changes')


def get_xblock_lms_link(usage_key):
    lms_link = get_lms_link_for_item(usage_key).strip('//')
    lms_link = lms_link.replace(settings.LMS_BASE, settings.LMS_ROOT_URL)
    return lms_link


def _handle_handout_changes(xblock, old_content):
    """
    This function is responsible for generating email data for any type of handout changes and will send the email to
    enrolled students.

    Arguments:
        xblock: Update handouts xblock
        old_content: Old content of the handout xblock
    """
    old_content_with_absolute_urls = None
    if old_content:
        # Whenever new course is created old_content is None.
        # Operations for old xblock data
        old_content_with_replaced_static_urls = replace_static_urls(
            old_content.get('data'),
            course_id=xblock.location.course_key)
        absolute_urls_of_old_data = _get_urls(old_content_with_replaced_static_urls)
        old_content_with_absolute_urls = _replace_relative_urls_with_absolute_urls(
            old_content_with_replaced_static_urls,
            absolute_urls_of_old_data)

    # Operations for New Xblock Data
    new_content_with_replaced_static_urls = replace_static_urls(xblock.data, course_id=xblock.location.course_key)
    absolute_urls_of_new_data = _get_urls(new_content_with_replaced_static_urls)
    new_content_with_absolute_urls = _replace_relative_urls_with_absolute_urls(
        new_content_with_replaced_static_urls,
        absolute_urls_of_new_data)

    old_content_lines = []
    if old_content_with_absolute_urls:
        old_content_lines = old_content_with_absolute_urls.split('\n')
    old_content_lines = [line.strip() for line in old_content_lines]
    new_content_lines = new_content_with_absolute_urls.split('\n')
    new_content_lines = [line.strip() for line in new_content_lines]
    new_changes = get_new_changes(old_content_lines, new_content_lines)
    if len(new_changes):
        email_params = get_email_params(xblock)
        email_params['new_content'] = new_changes
        students = get_course_enrollments(xblock.location.course_key)
        send_bulk_mail_to_students.delay(students, email_params, 'handout_changes')


def get_new_changes(old_content_lines, new_content_lines):
    changes = []
    for line in new_content_lines:
        if line not in old_content_lines:
            changes.append(line)
    return '\n'.join(changes)


def _replace_relative_urls_with_absolute_urls(content, absolute_urls):
    """
    This function will replace the all relative url from the given content to the absolute urls

    Arguments:
        content: Content to be changed
        absolute_urls: List of absolute urls to change with relative urls.

    Returns:
        Updated content contains all absolute urls.
    """
    for relative_url, absolute_url in absolute_urls.items():
        content = content.replace(relative_url, absolute_url)
    return content


def _get_urls(content):
    """
    This function will extract the relative urls from content

    Arguments:
        content: String from which we have to extract the relative imports

    Returns:
        List of relative urls
    """
    absolute_urls = {}
    pattern = r'href\s*=\s*("/asset[:.A-z0-9/+@-]*")'
    try:
        relative_urls = re.findall(pattern, content)
        for relative_url in relative_urls:
            absolute_urls[relative_url] = '"{}{}"'.format(
                                                        settings.LMS_ROOT_URL,
                                                        relative_url.replace('"', ''))
    except TypeError:
        # If new course created the old_content will be None or Empty
        return {}
    return absolute_urls


def get_course_enrollments(course_id):
    """
    This function will get all of the students enrolled in the specific course.

    Arguments:
        course_id: id of the specific course.
    Returns:
        List of the enrolled students.
    """
    course_enrollments = CourseEnrollment.objects.filter(course_id=course_id, is_active=True)
    students = [enrollment.user.id for enrollment in course_enrollments]
    return students


def update_context_with_thread(context, thread):
    thread_author = User.objects.get(id=thread.user_id)
    context.update({
        'thread_id': thread.id,
        'thread_title': thread.title,
        'thread_body': thread.body,
        'thread_commentable_id': thread.commentable_id,
        'thread_author_id': thread_author.id,
        'thread_username': thread_author.username,
        'thread_created_at': thread.created_at
    })


def update_context_with_comment(context, comment):
    comment_author = User.objects.get(id=comment.user_id)
    context.update({
        'comment_id': comment.id,
        'comment_body': comment.body,
        'comment_author_id': comment_author.id,
        'comment_username': comment_author.username,
        'comment_created_at': comment.created_at
    })


def build_message_context(context):
    site = context['site']
    message_context = get_base_template_context(site)
    message_context.update(context)
    message_context.update({
        'site_id': site.id,
        'post_link': _get_thread_url(context),
        'course_name': CourseOverview.get_from_id(message_context.pop('course_id')).display_name
    })
    message_context.pop('site')
    return message_context


def is_notification_configured_for_site(site, post_id):
    if site is None:
        log.info('Discussion: No current site, not sending notification about new thread: %s.', post_id)
        return False
    try:
        if not site.configuration.get_value(ENABLE_FORUM_NOTIFICATIONS_FOR_SITE_KEY, False):
            log_message = 'Discussion: notifications not enabled for site: %s. Not sending message about new thread: %s'
            log.info(log_message, site, post_id)
            return False
    except SiteConfiguration.DoesNotExist:
        log_message = 'Discussion: No SiteConfiguration for site %s. Not sending message about new thread: %s.'
        log.info(log_message, site, post_id)
        return False
    return True


def send_comments_reply_email_to_comment_owner(comment, context):
    """
    This function will send a notification email to Comment Owner in case of reply on a comment.

    By default edX is sending email to Thread Owner only. We have extended this functionality
    as we want to send an email to comment owner also.

    Arguments:
        comment: Replied Comment.
        context: Data to be sent to the email
    """
    context.update({
        'course_id': CourseKey.from_string(comment.course_id),
    })
    update_context_with_thread(context, comment.thread)
    update_context_with_comment(context, comment)
    message_context = build_message_context(context)
    parent_comment = Comment(id=comment.parent_id).retrieve()
    if parent_comment.user_id != comment.thread.user_id and parent_comment.user_id != comment.user_id:
        recipients = [parent_comment.user_id]
        send_bulk_mail_to_students.delay(recipients, message_context, 'comment_reply')


def get_course_name_from_id(course_id):
    try:
        course_key = CourseKey.from_string(course_id)
        return CourseOverview.get_from_id(course_key).display_name
    except (CourseOverview.DoesNotExist, InvalidKeyError):
        return None


def send_contact_us_email(data):
    payload = json.dumps(data)
    email_status = False
    recipient_list = settings.CONTACT_US_SUPPORT_EMAILS
    current_site = get_current_site()
    message_context = get_base_template_context(current_site)
    course_name = get_course_name_from_id(data['custom_fields'][0]['value'])

    message_context.update({
        'requester_name': data['requester']['name'],
        'requester_email': data['requester']['email'],
        'body': data['comment']['body'],
        'custom_fields': data['custom_fields'],
        'subject': data['subject']
    })

    if course_name:
        message_context.update({'course_name': course_name})

    for recipient_email in recipient_list:
        log.info(
            u'Attempting to send contact_us email to: %s of support team, For context: %s',
            recipient_email,
            payload
        )
        message = ContactUsSupportNotification().personalize(
            recipient=Recipient(username='', email_address= recipient_email),
            language='en',
            user_context=message_context,
        )
        try:
            ace.send(message)
            log.info(
                u'Success: sending contact-us email to: %s of support team, For context: %s',
                recipient_email,
                payload
            )
            email_status = True
        except Exception:  # pylint: disable=broad-except
            log.exception(
                u'Failure: sending contact-us email to: %s of support team, For context: %s',
                recipient_email,
                payload
            )
    return email_status


def generate_custom_ora2_report(header, datarows):
    """
    Add/Update data of csv file to export as a ORA2 report.

    Arguments:
        header: Header row of csv file.
        datarows: List of rows of Data to be written in csv.
    """
    try:
        response_index = header.index('Response')
        submission_uuid_index = header.index('Submission ID')
        anonymous_user_id_index = header.index('Anonymized Student ID')
        assessment_title_index = submission_uuid_index + 1
        header[anonymous_user_id_index] = "Username"
        header[assessment_title_index:assessment_title_index] = ["Assessment Title"]

        for row in datarows:
            # Replacing anonymous user id with username
            submitter = user_by_anonymous_id(row[anonymous_user_id_index])
            if submitter:
                row[anonymous_user_id_index] = submitter.username

            # Replacing the file_keys with downloadable urls
            response = row[response_index]
            file_keys = response.get('file_keys')
            if file_keys:
                file_keys = replace_file_key_with_downloadable_url(file_keys)
                response['file_keys'] = file_keys

            # Fetching Assessment title and adding it to csv
            submission_uuid = row[submission_uuid_index]
            submission_object = _get_submission_model(submission_uuid)
            if submission_object:
                item_id = submission_object.student_item.item_id
                try:
                    block_usage_key = UsageKey.from_string(item_id)
                    assessment_block = modulestore().get_item(block_usage_key)
                    assessment_title = assessment_block.title
                    row[assessment_title_index:assessment_title_index] = [assessment_title]
                except ItemNotFoundError:
                    log.info('Item with key:{usage_key} not Found'.format(usage_key=block_usage_key))
                    row[assessment_title_index:assessment_title_index] = ["Deleted/Not Available"]
                except Submission.DoesNotExist:
                    log.exception('Submission object not Found!!')
                except InvalidKeyError:
                    log.exception('Usage Key for the given block is not found')
    except ValueError:
        log.exception('Header is not Available in CSV Headers received from ORA2.')
    except FileUploadInternalError:
        log.exception('An internal exception occurred while generating a download URL.')

    return header, datarows


def replace_file_key_with_downloadable_url(file_keys):
    """
    Transform file keys to downloadable s3 links.

    Arguments:
        file_keys: Unique file key of s3 bucket.
    """
    backend = backends.get_backend()
    downloadable_file_urls = []
    for file_key in file_keys:
        downloadable_file_urls.append(backend.get_download_url(file_key))
    return downloadable_file_urls
