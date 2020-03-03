"""
Module working as a facade to all business rules from the entire system.
It must interact only with app's internal facades and can be used by views, CLI and other interfaces
"""
from logging import Logger

import requests
from activecampaign.exception import ActiveCampaignError as _ActiveCampaignError
from celery import shared_task
from django.conf import settings, settings as _settings
from django.core.mail import send_mail as _send_mail
from django.template.loader import render_to_string
from django.urls import reverse

from pythonpro.absolute_uri import build_absolute_uri
from pythonpro.cohorts import facade as _cohorts_facade
from pythonpro.core import facade as _core_facade
from pythonpro.core.models import User as _User
from pythonpro.discourse.facade import MissingDiscourseAPICredentials, generate_sso_payload_and_signature
from pythonpro.email_marketing import facade as _email_marketing_facade
from pythonpro.payments import facade as _payments_facade

_logger = Logger(__file__)

UserCreationException = _core_facade.UserCreationException  # exposing exception on Facade

__all__ = [
    'register_lead', 'force_register_client', 'promote_client', 'activate_user', 'find_user_interactions',
    'visit_client_landing_page', 'visit_member_landing_page', 'run_pytools_promotion_campaign', 'click_client_checkout',
    'client_generated_boleto', 'promote_member', 'find_user_by_email', 'find_user_by_id', 'force_register_lead',
    'subscribe_to_waiting_list', 'force_register_member', 'click_member_checkout'
]

CLIENT_BOLETO_TAG = 'client-boleto'


def register_lead(first_name: str, email: str, source: str = 'unknown') -> _User:
    """
    Create a new user on the system generation a random password.
    An Welcome email is sent to the user informing his password with the link to change it.
    User is also registered on Mailchimp and subscribed to LeadWorkflow and is not registered on system in case api call
    fails

    :param first_name: User's first name
    :param email: User's email
    :param source: source of User traffic
    :return: User
    """
    if not source:
        source = 'unknown'
    form = _core_facade.validate_user(first_name, email, source)
    try:
        _email_marketing_facade.create_or_update_lead.delay(first_name, email)
    except _ActiveCampaignError:
        form.add_error('email', 'Email Inválido')
        raise UserCreationException(form)
    lead = _core_facade.register_lead(first_name, email, source)
    sync_user_on_discourse.delay(lead.id)
    _email_marketing_facade.create_or_update_lead.delay(first_name, email, id=lead.id)

    return lead


def force_register_lead(first_name: str, email: str, source: str = 'unknown') -> _User:
    """
    Create a new user on the system generation a random password.
    An Welcome email is sent to the user informing his password with the link to change it.
    User is also registered on Mailchimp. But she will be registeres even if api call fails
    :param first_name: User's first name
    :param email: User's email
    :param source: source of User traffic
    :return: User
    """
    user = _core_facade.register_lead(first_name, email, source)
    sync_user_on_discourse(user)
    try:
        _email_marketing_facade.create_or_update_lead.delay(first_name, email, id=user.id)
    except _ActiveCampaignError:
        pass
    return user


def force_register_client(first_name: str, email: str, source: str = 'unknown') -> _User:
    """
    Create a new user on the system generation a random password or update existing one based on email.
    An Welcome email is sent to the user informing his password with the link to change it.
    User is also registered on Mailchimp. But she will be registered even if api call fails
    :param first_name: User's first name
    :param email: User's email
    :param source: source of User traffic
    :return: User
    """
    user = _core_facade.register_client(first_name, email, source)
    sync_user_on_discourse(user)
    try:
        _email_marketing_facade.create_or_update_client(first_name, email, id=user.id)
    except _ActiveCampaignError:
        pass
    return user


def force_register_member(first_name, email, source='unknown'):
    """
    Create a new user on the system generation a random password or update existing one based on email.
    An Welcome email is sent to the user informing his password with the link to change it.
    User is also registered on Mailchimp. But she will be registered even if api call fails
    :param first_name: User's first name
    :param email: User's email
    :param source: source of User traffic
    :return: User
    """
    user = _core_facade.register_member(first_name, email, source)
    _cohorts_facade.subscribe_to_last_cohort(user)
    cohort = _cohorts_facade.find_most_recent_cohort()
    sync_user_on_discourse(user)
    try:
        _email_marketing_facade.create_or_update_member(first_name, email, id=user.id)
        _email_marketing_facade.tag_as(email, user.id, f'turma-{cohort.slug}')
    except _ActiveCampaignError:
        pass
    return user


def promote_member(user: _User, source: str) -> _User:
    """
    Promote a user to Member role and change it's role on Mailchimp. Will not fail in case API call fails.
    Email welcome email is sent to user
    :param source: source of traffic
    :param user:
    :return:
    """
    _core_facade.promote_to_member(user, source)
    _cohorts_facade.subscribe_to_last_cohort(user)
    cohort = _cohorts_facade.find_most_recent_cohort()
    sync_user_on_discourse(user)
    try:
        _email_marketing_facade.create_or_update_member(user.first_name, user.email, id=user.id)
        _email_marketing_facade.tag_as(user.email, user.id, f'turma-{cohort.slug}')
    except _ActiveCampaignError:
        pass
    email_msg = render_to_string(
        'payments/membership_email.txt',
        {
            'user': user,
            'cohort_detail_url': build_absolute_uri(cohort.get_absolute_url())
        }
    )
    _send_mail(
        f'Inscrição na Turma {cohort.title} realizada! Confira o link com detalhes.',
        email_msg,
        _settings.DEFAULT_FROM_EMAIL,
        [user.email]
    )
    return user


def promote_client(user: _User, source: str) -> None:
    """
    Promote a user to Client role and change it's role on Mailchimp. Will not fail in case API call fails.
    Email welcome email is sent to user
    :param source: source of traffic
    :param user:
    :return:
    """
    _core_facade.promote_to_client(user, source)
    sync_user_on_discourse(user)
    try:
        _email_marketing_facade.create_or_update_client(user.first_name, user.email, id=user.id)
    except _ActiveCampaignError:
        pass
    email_msg = render_to_string(
        'payments/pytools_email.txt',
        {
            'user': user,
            'ty_url': build_absolute_uri(reverse('payments:pytools_thanks'))
        }
    )
    _send_mail(
        'Inscrição no curso Pytools realizada! Confira o link com detalhes.',
        email_msg,
        _settings.DEFAULT_FROM_EMAIL,
        [user.email]
    )


def promote_client_and_remove_boleto_tag(user: _User, source: str = None):
    promote_client(user, source)
    _email_marketing_facade.remove_tags(user.email, user.id, CLIENT_BOLETO_TAG)


def find_user_by_email(user_email: str) -> _User:
    """
    Find user by her email
    :param user_email:
    :return: User
    """
    return _core_facade.find_user_by_email(user_email)


def find_user_by_id(user_id: int) -> _User:
    """
    Find user by her id
    :param user_id:
    :return:
    """
    return _core_facade.find_user_by_id(user_id)


def find_user_interactions(user: _User):
    """
    Find all user interactions ordered by creation date desc
    :param user:
    :return: list of user interactions
    """
    return _core_facade.find_user_interactions(user)


def run_pytools_promotion_campaign() -> int:
    """
    Run pytools campaign for users registered 7 weeks ago
    :return: number of user's marked for promotion
    """
    begin, end = _payments_facade.calculate_7th_week_before_promotion()
    promotion_users = _core_facade.find_leads_by_date_joined_interval(begin, end)
    for user in promotion_users:
        try:
            _email_marketing_facade.tag_as(user.email, user.id, 'pytools-promotion')
        except _ActiveCampaignError:
            pass
    return len(promotion_users)


def visit_client_landing_page(user: _User, source: str) -> None:
    """
    Marke user as visited client landing page
    :param source: string containing source of traffic
    :param user:
    :return:
    """
    _core_facade.visit_client_landing_page(user, source)
    _email_marketing_facade.tag_as(user.email, user.id, 'potential-client')


def visit_member_landing_page(user, source):
    """
    Mark user as visited member landing page
    :param source: string containing source of traffic
    :param user:
    :return:
    """
    _core_facade.visit_member_landing_page(user, source)
    try:
        _email_marketing_facade.tag_as(user.email, user.id, 'potential-member')
    except _ActiveCampaignError:
        pass  # Ok not handling, probably invalid email


def visit_launch_landing_page(user, source):
    """
    Mark user as visited launch landing page
    :param source: string containing source of traffic
    :param user:
    :return:
    """
    _core_facade.visit_launch_landing_page(user, source)


def subscribe_launch_landing_page(user, source):
    """
    Mark user as subscribed to launch
    :param source: string containing source of traffic
    :param user:
    :return:
    """
    _core_facade.subscribe_to_launch(user, source)


def click_member_checkout(user):
    """
    Mark user as visited client landing page
    :param user:
    :return:
    """
    _core_facade.member_checkout(user, None)
    _email_marketing_facade.tag_as(user.email, user.id, 'member-checkout')


def click_client_checkout(user: _User):
    """
    Mark user as visited client landing page
    :param user:
    :return:
    """
    _core_facade.client_checkout(user, None)
    _email_marketing_facade.tag_as(user.email, user.id, 'client-checkout')


def client_generated_boleto(user):
    """
        Mark user as visited generated boleto
        :param user:
        :return:
        """
    _email_marketing_facade.tag_as(user.email, user.id, CLIENT_BOLETO_TAG)
    _core_facade.client_generated_boleto(user, None)


def member_generated_boleto(user):
    _core_facade.member_generated_boleto(user, None)


def subscribe_to_waiting_list(user: _User, source: str) -> None:
    """
    Subscribe user to waiting list
    :param user:
    :param source:
    :return:
    """
    _core_facade.subscribe_to_waiting_list(user, source)
    _email_marketing_facade.tag_as(user.email, user.id, 'lista-de-espera')


def activate_user(user: _User, source: str) -> None:
    """
    Activate user
    :param user:
    :param source:
    :return:
    """
    _core_facade.activate_user(user, source)
    _email_marketing_facade.remove_tags.delay(user.email, user.id, 'never-watched-video')


def visit_cpl1(user: _User, source: str) -> None:
    """
    User visit CPL1
    :param user:
    :param source:
    :return:
    """
    _core_facade.visit_cpl1(user, source)
    _email_marketing_facade.tag_as(user.email, user.id, 'cpl1')


def visit_cpl2(user: _User, source: str) -> None:
    """
    User visit CPL2
    :param user:
    :param source:
    :return:
    """
    _core_facade.visit_cpl2(user, source)
    _email_marketing_facade.tag_as(user.email, user.id, 'cpl2')


def visit_cpl3(user: _User, source: str) -> None:
    """
    User visit CPL2
    :param user:
    :param source:
    :return:
    """
    _core_facade.visit_cpl3(user, source)
    _email_marketing_facade.tag_as(user.email, user.id, 'cpl3')


@shared_task()
def sync_user_on_discourse(user_or_id):
    """
    Synchronize user data on forum if API is configured
    :param user_or_id: Django user or his id
    :return: returns result of hitting Discourse api
    """
    can_make_api_call = bool(settings.DISCOURSE_API_KEY and settings.DISCOURSE_API_USER)
    can_work_without_sync = not (settings.DISCOURSE_BASE_URL or can_make_api_call)
    if can_work_without_sync:
        _logger.info('Discourse Integration not available')
        return
    elif not can_make_api_call:
        raise MissingDiscourseAPICredentials('Must define both DISCOURSE_API_KEY and DISCOURSE_API_USER configs')

    user = _core_facade.find_user_by_id(user_or_id)

    # https://meta.discourse.org/t/sync-sso-user-data-with-the-sync-sso-route/84398
    params = {
        'email': user.email,
        'external_id': user.id,
        'require_activation': 'false',
        'groups': ','.join(g.name for g in user.groups.all())
    }
    sso_payload, signature = generate_sso_payload_and_signature(params)
    # query_string = parse.urlencode()
    url = f'{settings.DISCOURSE_BASE_URL}/admin/users/sync_sso'
    headers = {
        'content-type': 'multipart/form-data',
        'Api-Key': settings.DISCOURSE_API_KEY,
        'Api-Username': settings.DISCOURSE_API_USER,
    }

    requests.post(url, data={'sso': sso_payload, 'sig': signature}, headers=headers)
