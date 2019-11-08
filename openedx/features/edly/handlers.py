from six import text_type

from django.conf import settings
from django.dispatch import receiver
from django_comment_common import signals as forum_signals
from lms.djangoapps.instructor.enrollment import get_email_params
from opaque_keys.edx.keys import CourseKey
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from openedx.core.djangoapps.theming.helpers import get_current_site
from openedx.features.edly.tasks import send_bulk_mail_to_students, send_course_enrollment_mail
from openedx.features.edly.utils import (
    build_message_context,
    get_course_enrollments,
    is_notification_configured_for_site,
    update_context_with_comment,
    update_context_with_thread
)


def handle_user_enrollment(course_id, user, enrollment_state):
    """
    Handle the course enrollment and send the email to the student about enrollment.

    Arguments:
        course_id: id of course in which user enrolled
        user: User enrolled in course
        enrollment_state: 'enroll' or 'unenroll'
    """
    email_params = {}
    if enrollment_state == "enroll":
        email_params['message_type'] = 'enrolled_enroll'
    elif enrollment_state == "unenroll":
        email_params['message_type'] = 'enrolled_unenroll'

    site = get_current_site()
    site_id = ''
    if site:
        site_id = site.id
    email_params['site_id'] = site_id

    user_fullname = user.profile.name
    user_email = user.email
    course = CourseOverview.objects.get(id=course_id)

    email_params.update(get_email_params(course, True, secure=False))
    email_params['contact_mailing_address'] = settings.CONTACT_MAILING_ADDRESS
    email_params['email_address'] = user_email
    email_params['full_name'] = user_fullname
    email_params['enroll_by_self'] = True
    email_params['course'] = text_type(email_params['course'])

    send_course_enrollment_mail.delay(user_email, email_params)


@receiver(forum_signals.thread_created)
def send_thread_create_email_notification(sender, user, post, **kwargs):
    """
    This function will send a new thread notification email to all course enrolled students.

    Arguments:
        sender: Model from which we received signal (we are not using it in this case).
        user: Thread owner
        post: Thread that is being created
        kwargs: Remaining key arguments of signal.
    """
    current_site = get_current_site()
    if not is_notification_configured_for_site(current_site, post.id):
        return
    course_key = CourseKey.from_string(post.course_id)
    context = {
        'site': current_site,
        'course_id': course_key
    }
    update_context_with_thread(context, post)
    message_context = build_message_context(context)
    receipients = get_course_enrollments(course_key)
    send_bulk_mail_to_students.delay(receipients, message_context, 'new_thread')


@receiver(forum_signals.thread_voted)
def send_vote_email_notification(sender, user, post, undo_vote=False, **kwargs):
    """
    This handler will be called on both signals thread_vote and comment_vote.
    It will send a vote notification email to thread owner or comment owner.

    Arguments:
        sender: Model from which we received signal (we are not using it in this case).
        user: Voter
        post: Thread or Comment that is being voted
        undo_vote: Flag indicating whether user has voted it or has removed his vote
        kwargs: Remaining key arguments of signal
    """
    if undo_vote:
        return
    current_site = get_current_site()
    if not is_notification_configured_for_site(current_site, post.id):
        return
    course_key = CourseKey.from_string(post.course_id)
    context = {
        'site': current_site,
        'course_id': course_key,
        'voter_name': user.username,
        'voter_email': user.email
    }
    notification_object_type = "thread_vote"
    recipients = [post.user_id]
    if post.type == "comment":
        update_context_with_thread(context, post.thread)
        update_context_with_comment(context, post)
        notification_object_type = "comment_vote"
    else:
        update_context_with_thread(context, post)
    message_context = build_message_context(context)
    send_bulk_mail_to_students.delay(recipients, message_context, notification_object_type)
