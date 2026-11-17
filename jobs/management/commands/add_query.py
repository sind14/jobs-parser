from django.core.management.base import BaseCommand

from jobs.models import SearchQuery


class Command(BaseCommand):
    help = 'Create a search query from CLI: add_query "Python Developer" "United States"'

    def add_arguments(self, parser):
        parser.add_argument("keywords")
        parser.add_argument("location")
        parser.add_argument("--geo-id", default="")
        parser.add_argument("--time-posted", default="", choices=["", "r86400", "r604800", "r2592000"])
        parser.add_argument("--max-jobs", type=int)

    def handle(self, *args, **o):
        q, created = SearchQuery.objects.get_or_create(
            keywords=o["keywords"], location=o["location"], time_posted=o["time_posted"],
            defaults={"geo_id": o["geo_id"], "max_jobs": o["max_jobs"]},
        )
        self.stdout.write(f"{'Created' if created else 'Exists'}: #{q.pk} {q}")
