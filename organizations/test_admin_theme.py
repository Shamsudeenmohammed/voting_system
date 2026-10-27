"""Back-office theme (django-jazzmin) smoke tests.

The Django admin is mounted at ``/watch/`` and skinned by Jazzmin.  These tests
pin the integration end to end so that a Django or Jazzmin upgrade that breaks
template resolution, static-file wiring or the custom sign-in page fails loudly
instead of silently falling back to the stock admin.
"""

from django.contrib.auth import get_user_model
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class BackOfficeThemeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.staff = User.objects.create_user(
            "backoffice", password="pass-12345", is_staff=True, is_superuser=True
        )
        cls.non_staff = User.objects.create_user("voter", password="pass-12345")

    # -- Custom sign-in page -------------------------------------------------

    def test_admin_login_uses_the_custom_template(self):
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/login.html")
        content = response.content.decode()
        # Markers unique to the project template (not Jazzmin's stock one).
        self.assertIn("Back to the voting site", content)
        self.assertIn("img/brand-mark.svg", content)

    def test_admin_login_defaults_back_to_the_back_office(self):
        # No deep link supplied: the sign-in page should send staff to the
        # admin dashboard it was reached from, not the product console.
        response = self.client.get(reverse("admin:login"))
        self.assertContains(response, f'name="next" value="{reverse("admin:index")}"')

    def test_staff_can_sign_in_through_the_admin_login(self):
        response = self.client.post(
            reverse("admin:login"),
            {
                "username": "backoffice",
                "password": "pass-12345",
                "next": reverse("admin:index"),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("admin:index"))

    def test_admin_login_rejects_non_staff_credentials(self):
        response = self.client.post(
            reverse("admin:login"),
            {"username": "voter", "password": "pass-12345"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.assertContains(response, "correct username and password")

    # -- Themed admin shell --------------------------------------------------

    def test_admin_index_renders_inside_the_jazzmin_shell(self):
        self.client.force_login(self.staff)
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("jazzy-sidebar", content)          # Jazzmin layout
        self.assertIn("css/admin_theme.css", content)    # custom_css wired up
        self.assertIn("fas fa-poll", content)            # per-model icon
        self.assertIn("Elections", content)              # app group label

    def test_registered_changelists_render(self):
        self.client.force_login(self.staff)
        for url_name in (
            "admin:elections_election_changelist",
            "admin:elections_voter_changelist",
            "admin:organizations_organization_changelist",
            "admin:audit_auditevent_changelist",
            "admin:auth_user_changelist",
        ):
            with self.subTest(url_name=url_name):
                response = self.client.get(reverse(url_name))
                self.assertEqual(response.status_code, 200)

    def test_theme_assets_are_discoverable(self):
        for asset in ("css/admin_theme.css", "img/brand-mark.svg"):
            with self.subTest(asset=asset):
                self.assertIsNotNone(finders.find(asset))
