from django.conf import settings
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django_comment_common import signals as forum_signals
from lms.djangoapps.instructor.enrollment import get_email_params
from opaque_keys.edx.keys import CourseKey
from openedx.core.djangoapps.theming.helpers import get_current_site
from openedx.features.edly.tasks import send_bulk_mail_to_students, send_course_enrollment_mail
from openedx.features.edly.utils import (
    build_message_context,
    get_course_enrollments,
    is_notification_configured_for_site,
    update_context_with_comment,
    update_context_with_thread
)
from student.models import CourseEnrollment


@receiver(post_save, sender=CourseEnrollment)
def handle_user_enrollment(sender, instance, **kwargs):
    """
    Handle the course enrollment and send the email to the student about enrollment.

    :param sender: Model from which we received signal.
    :param instance: Instance of model which has been created or updated
    :param kwargs: Remaining parts of signal.
    """

    # This if condition check's for if the Enrollment signal is created/updated using LMS or not.
    # We are handling only lms site.

    if settings.ROOT_URLCONF == 'lms.urls':
        user_fullname = instance.user.profile.name
        user_email = instance.user.email
        email_params = get_email_params(instance.course, True, secure=False)
        email_params['email_address'] = user_email
        email_params['full_name'] = user_fullname
        email_params['enroll_by_self'] = True
        if instance.is_active:
            email_params['message_type'] = 'enrolled_enroll'
        elif not instance.is_active and not kwargs['created']:
            email_params['message_type'] = 'enrolled_unenroll'
        else:
            return
        send_course_enrollment_mail.delay(user_email, email_params)


@receiver(forum_signals.thread_created)
def send_thread_create_email_notification(sender, user, post, **kwargs):
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
def send_vote_email_notification(sender, user, post, **kwargs):
    if kwargs.get('undo_vote', False):
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
    recipients = [User.objects.get(id=post.user_id)]
    if post.type == "comment":
        update_context_with_thread(context, post.thread)
        update_context_with_comment(context, post)
        notification_object_type = "comment_vote"
    else:
        update_context_with_thread(context, post)
    message_context = build_message_context(context)
    send_bulk_mail_to_students.delay(recipients, message_context, notification_object_type)
