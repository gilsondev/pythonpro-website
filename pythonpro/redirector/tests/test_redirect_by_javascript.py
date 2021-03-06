import pytest
from django.urls import reverse
from model_bakery import baker

from pythonpro.django_assertions import dj_assert_contains
from pythonpro.redirector.models import Redirect
from pythonpro.redirector.facade import get_redirect_url


@pytest.fixture
def redirect(db):
    return baker.make(Redirect, url='https://google.com', use_javascript=True)


def test_should_redirect_url_in_redirect_object(redirect):
    assert get_redirect_url(redirect) == redirect.url


@pytest.fixture
def resp(client, redirect):
    return client.get(reverse('redirector:redirect', kwargs={'slug': redirect.slug}))


def test_status_code_should_return_200(resp):
    assert resp.status_code == 200


def test_should_redirect_js_contains_redirect_url(resp, redirect):
    dj_assert_contains(resp, f'window.location.replace("{redirect.url}")')
