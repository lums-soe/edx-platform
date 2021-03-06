"""
Tests for third_party_auth utility functions.
"""
from __future__ import absolute_import

import unittest

from django.conf import settings

from student.tests.factories import UserFactory
from third_party_auth.tests.testutil import TestCase
from third_party_auth.utils import user_exists


@unittest.skipUnless(settings.ROOT_URLCONF == 'lms.urls', 'Test only valid in lms')
class TestUtils(TestCase):
    """
    Test the utility functions.
    """
    def test_user_exists(self):
        """
        Verify that user_exists function returns correct response.
        """
        # Create users from factory
        UserFactory(username='test_user', email='test_user@example.com')
        self.assertTrue(
            user_exists({'username': 'test_user', 'email': 'test_user@example.com'}),
        )
        self.assertTrue(
            user_exists({'username': 'test_user'}),
        )
        self.assertTrue(
            user_exists({'email': 'test_user@example.com'}),
        )
        self.assertFalse(
            user_exists({'username': 'invalid_user'}),
        )
        self.assertTrue(
            user_exists({'username': 'TesT_User'})
        )
