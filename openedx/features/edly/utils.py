import logging
import re

from courseware.access import has_access
from courseware.courses import get_course_by_id
from django.conf import settings
from django.contrib.auth.models import User
from edx_ace.utils import date
from lms.djangoapps.discussion.tasks import _get_thread_url
from lms.lib.comment_client.comment import Comment
from opaque_keys.edx.keys import CourseKey
from openedx.core.djangoapps.ace_common.template_context import get_base_template_context
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers
from openedx.core.djangoapps.site_configuration.models import SiteConfiguration
from openedx.core.djangoapps.theming.helpers import get_current_site
from openedx.features.edly.tasks import send_bulk_mail_to_students
from student.models import CourseEnrollment
from xmodule.contentstore.content import StaticContent
from xmodule.modulestore.django import modulestore
from xmodule.modulestore.exceptions import InvalidLocationError, ItemNotFoundError

log = logging.getLogger(__name__)
ENABLE_FORUM_NOTIFICATIONS_FOR_SITE_KEY = 'enable_forum_notifications'
COURSE_OUTLINE_CATEGORIES = ['vertical', 'sequential', 'chapter']


def notify_students_about_xblock_changes(xblock, publish, old_content):
    """
    This function is responsible for calling the related function by checking the xblock category.

    :param xblock: Block of courses which has been published.
    :param publish: Param to check if xblock is going to be published.
    :param old_content: Old data of xblock before updating.
    """
    if (publish == 'make_public' and xblock.category in COURSE_OUTLINE_CATEGORIES
            and not xblock.visible_to_staff_only):
        _handle_section_publish(xblock)
    elif xblock.category == 'course_info' and xblock.location.block_id == 'handouts':
        _handle_handout_changes(xblock, old_content)


def get_email_params(xblock):
    """
    Generates the email params for any changes in course

    :param xblock: xblock which is modified/created
    :return: Dict containing the data for email.
    """
    email_params = {}
    course = get_course_by_id(xblock.location.course_key)
    email_params['site'] = get_current_site()
    email_params['course_url'] = _get_course_url(xblock.location.course_key)
    email_params['course_name'] = course.display_name_with_default
    email_params['display_name'] = xblock.display_name
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

    :param xblock: xblock which is modified/created
    """
    email_params = get_email_params(xblock)
    students = get_course_enrollments(xblock.location.course_key)
    students = check_students_permission(xblock, students)

    if xblock.category == 'vertical':
        published_unit_url = _get_published_unit_url(xblock)
        email_params['change_type'] = 'Unit'
        email_params['change_url'] = published_unit_url
    elif xblock.category == 'sequential':
        subsection_url = '{lms_base_url}/courses/{course_id}/course/#{subsection_usage_key}'.format(
            lms_base_url=settings.LMS_ROOT_URL,
            course_id=xblock.location.course_key,
            subsection_usage_key=xblock.location
        )
        email_params['change_url'] = subsection_url
        email_params['change_type'] = 'Sub Section'
    else:
        section_url = '{lms_base_url}/courses/{course_id}/course/#{section_usage_key}'.format(
            lms_base_url=settings.LMS_ROOT_URL,
            course_id=xblock.location.course_key,
            section_usage_key=xblock.location
        )
        email_params['change_type'] = 'Section'
        email_params['change_url'] = section_url
    send_bulk_mail_to_students.delay(students, email_params, 'outline_changes')


def _handle_handout_changes(xblock, old_content):
    """
    This function is responsible for generating email data for any type of handout changes and will send the email to
    enrolled students.

    :param xblock: Update handouts xblock
    :param old_content: Old content of the handout xblock
    """
    # Operations for New Xblock Data
    relative_urls_of_new_data = _get_relative_urls(xblock.data)
    relative_urls_of_new_data, absolute_urls_of_new_data = _create_absolute_urls(xblock.location.course_key,
                                                                                 relative_urls_of_new_data)
    new_content_with_absolute_urls = _replace_relative_urls_with_absolute_urls(xblock.data,
                                                                               relative_urls_of_new_data,
                                                                               absolute_urls_of_new_data)

    # Operations for old xblock data
    relative_urls_of_old_data = _get_relative_urls(old_content.get('data', []))
    relative_urls_of_old_data, absolute_urls_of_old_data = _create_absolute_urls(xblock.location.course_key,
                                                                                 relative_urls_of_old_data)
    old_content_with_absolute_urls = _replace_relative_urls_with_absolute_urls(old_content.get('data'),
                                                                               relative_urls_of_old_data,
                                                                               absolute_urls_of_old_data)

    email_params = get_email_params(xblock)
    email_params['old_content'] = old_content_with_absolute_urls
    email_params['new_content'] = new_content_with_absolute_urls
    students = get_course_enrollments(xblock.location.course_key)
    students = check_students_permission(xblock, students)
    send_bulk_mail_to_students.delay(students, email_params, 'handout_changes')


def _replace_relative_urls_with_absolute_urls(content, relative_urls, absolute_urls):
    """
    This function will replace the all relative url from the given content to the absolute urls

    :param content: Content to be changed
    :param relative_urls: List of relative urls to change from content
    :param absolute_urls: List of absolute urls to change with relative urls.
    :return: Updated content contains all absolute urls.
    """
    relative_urls = ['"{path}"'.format(path=path) for path in relative_urls]
    absolute_urls = ['"{path}"'.format(path=path) for path in absolute_urls]
    for x in range(0, len(relative_urls)):
        content = content.replace(relative_urls[x], absolute_urls[x])
    return content


def _get_relative_urls(content):
    """
    This function will extract the relative urls from content

    :param content: String from which we have to extract the relative imports
    :return: List of relative urls
    """
    pattern = r'href' '*=' '*("/[:.A-z0-9/+@-]*")'
    try:
        relative_urls = re.findall(pattern, content)
        relative_urls = [url.replace('"', '') for url in relative_urls]
    except TypeError:
        # If new course created the old_content will be None or Empty
        return []
    return relative_urls


def _create_absolute_urls(course_key, relative_urls):
    """
    This function will make the absolute urls of the given relative urls

    :param course_key: Course key for the assets
    :param relative_urls: list of relative urls
    :return: list of absolute urls
    """
    absolute_urls = []
    for relative_url in relative_urls:
        if _is_static_path(relative_url):
            absolute_url = _generate_absolute_url_from_static_path(course_key, relative_url)
            absolute_urls.append(absolute_url)
        elif _is_canonicalized_asset_path(relative_url):
            absolute_url = relative_url.replace('block@', 'block/')
            absolute_urls.append(absolute_url)
        else:
            # This url doesn't belong to the course we are not changing it.
            relative_urls.remove(relative_url)
    absolute_urls = ["{lms_base_url}{path}".format(lms_base_url=settings.LMS_ROOT_URL, path=path)
                     for path in absolute_urls]
    return relative_urls, absolute_urls


def _is_static_path(path):
    return path.startswith('/static/')


def _is_canonicalized_asset_path(path):
    return path.startswith('/asset')


def _generate_absolute_url_from_static_path(course_key, path):
    """
    This function will generate the exact path of the given static path.

    :param course_key: Course for which we have to create the static assets path.
    :param path: Static path of asset.
    :return: Absolute path of the static asset.
    """
    absolute_url = StaticContent.get_canonicalized_asset_path(course_key, path, "", {})
    absolute_url = absolute_url.replace('block@', 'block/')
    return absolute_url


def get_xblock(usage_key):
    """
    This function will get xblock.

    :param usage_key: usage_key of xblock we are getting.
    :return: xblock
    """
    store = modulestore()
    with store.bulk_operations(usage_key.course_key):
        try:
            return store.get_item(usage_key, depth=None)
        except ItemNotFoundError:
            log.error("Can't find parent xblock.")
            return None
        except InvalidLocationError:
            log.error("Can't find parent/item by location.")
            return None


def _get_published_unit_url(xblock):
    """
    This function will generate the url of unit published

    :param xblock: Unit xblock published
    :return: Learner site url of unit.

    URL pattern:  LMS_ROOT_URL/courses/courseware/SECTION_URL_NAME/SUBSECTION_URL_NAME/UNIT_URL_NAME/TAB_NUM?
                    activate_block_id=UNIT_XBLOCK_USAGE_KEY
    """
    published_unit_url = None
    if xblock.parent:
        subsection_xblock = get_xblock(xblock.parent)
        if subsection_xblock:
            section_xblock = get_xblock(subsection_xblock.parent)
            if section_xblock:
                subsection_url_name = subsection_xblock.url_name
                section_url_name = section_xblock.url_name
                published_unit_url = '{lms_base_url}/courses/{course_id}/courseware/{section_url_name}/' \
                                     '{subsection_url_name}/?activate_block_id={unit_usage_key}'.format(
                                        lms_base_url=settings.LMS_ROOT_URL,
                                        course_id=xblock.location.course_key,
                                        section_url_name=section_url_name,
                                        subsection_url_name=subsection_url_name,
                                        unit_usage_key=xblock.location)
    return published_unit_url


def get_course_enrollments(course_id):
    """
    This function will get all of the students enrolled in the specific course.

    :param course_id: id of the specific course.
    :return: List of the enrolled students.
    """
    course_enrollments = CourseEnrollment.objects.filter(course_id=course_id)
    students = [enrollment.user for enrollment in course_enrollments]
    return students


def check_students_permission(xblock, students):
    students_has_permission = []
    for student in students:
        if has_access(student, 'load', xblock, xblock.location.course_key):
            students_has_permission.append(student)
    return students_has_permission


def update_context_with_thread(context, thread):
    thread_author = User.objects.get(id=thread.user_id)
    context.update({
        'thread_id': thread.id,
        'thread_title': thread.title,
        'thread_body': thread.body,
        'thread_commentable_id': thread.commentable_id,
        'thread_author_id': thread_author.id,
        'thread_username': thread_author.username,
        'thread_created_at': date.deserialize(thread.created_at)
    })


def update_context_with_comment(context, comment):
    comment_author = User.objects.get(id=comment.user_id)
    context.update({
        'comment_id': comment.id,
        'comment_body': comment.body,
        'comment_author_id': comment_author.id,
        'comment_username': comment_author.username,
        'comment_created_at': date.deserialize(comment.created_at),
    })


def build_message_context(context):
    site = context['site']
    message_context = get_base_template_context(site)
    message_context.update(context)
    message_context.update({
        'site_id': site.id,
        'post_link': _get_thread_url(context),
        'course_name': CourseOverview.get_from_id(context['course_id']).display_name
    })
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
    context.update({
        'course_id': CourseKey.from_string(comment.course_id),
    })
    update_context_with_thread(context, comment.thread)
    update_context_with_comment(context, comment)
    message_context = build_message_context(context)
    parent_comment = Comment(id=comment.parent_id).retrieve()
    if parent_comment.user_id != comment.thread.user_id and parent_comment.user_id != comment.user_id:
        receipients = [User.objects.get(id=parent_comment.user_id)]
        send_bulk_mail_to_students.delay(receipients, message_context, 'comment_reply')
