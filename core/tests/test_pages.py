from django.contrib.staticfiles.finders import find
from django.test import SimpleTestCase


class PageTests(SimpleTestCase):
    def test_public_pages(self):
        home_response = self.client.get("/")

        self.assertEqual(home_response.status_code, 200)
        self.assertContains(
            home_response,
            '<header><a href="/" aria-label="Scopequote home">Scopequote</a></header>',
            html=True,
        )
        self.assertContains(
            home_response,
            '<a href="/estimator/">Estimate your project</a>',
            html=True,
        )
        self.assertIsNotNone(find("css/app.css"))

        health_response = self.client.get("/health/")

        self.assertEqual(health_response.status_code, 200)
        self.assertEqual(health_response.content, b"ok")
