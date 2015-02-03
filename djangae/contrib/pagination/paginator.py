from hashlib import md5
from django.conf import settings
from django.db import models
from django.core import paginator
from djangae.contrib.pagination.decorators import _field_name_for_ordering
from django.core.cache import cache


# TODO: it would be nice to be able to define a function which is given the queryset and returns
# the cache time.  That would allow different cache times for different queries.
CACHE_TIME = getattr(settings, "DJANGAE_PAGINATION_CACHE_TIME", 30*60)


class PaginationOrderingRequired(RuntimeError):
    pass


def _marker_cache_key(query_id, page_number):
    cache_key = "_PAGE_MARKER_{}:{}".format(query_id, page_number)
    return cache_key


def _count_cache_key(query_id):
    cache_key = "_PAGE_COUNTER_{}".format(query_id)
    return cache_key


def _update_known_count(query_id, count):
    cache_key = _count_cache_key(query_id)

    ret = cache.get(cache_key)
    if ret and ret > count:
        return

    cache.set(cache_key, count, CACHE_TIME)


def _get_known_count(query_id):
    cache_key = _count_cache_key(query_id)
    ret = cache.get(cache_key)
    if ret:
        return ret
    return 0

def _store_marker(query_id, page_number, marker_value):
    """
        For a model and query id, stores the marker value for previously
        queried page number.

        This stores the last item on the page identified by page number,
        not the marker that starts the page. i.e. there is a marker for page 1
    """

    cache_key = _marker_cache_key(query_id, page_number)
    cache.set(cache_key, marker_value, CACHE_TIME)


def _get_marker(query_id, page_number):
    """
        For a query_id, returns the marker at the end of the
        previous page. Returns a tuple of (marker, pages) where pages is
        the number of pages we had to go back to find the marker (this is the
        number of pages we need to skip in the result set)
    """

    counter = page_number - 1
    pages_skipped = 0

    while counter > 0   :
        cache_key = _marker_cache_key(query_id, counter)
        ret = cache.get(cache_key)

        if ret:
            return ret, pages_skipped

        counter -= 1
        pages_skipped += 1

    # If we get here then we couldn't find a stored marker anywhere
    return None, pages_skipped


def queryset_identifier(queryset):
    """ Returns a string that uniquely identifies this query excluding its low and high mark"""

    hasher = md5()
    hasher.update(queryset.model._meta.db_table)
    hasher.update(str(queryset.query.where))
    hasher.update(str(queryset.query.order_by))
    return hasher.hexdigest()


class Paginator(paginator.Paginator):
    """
        A paginator that works with the @paginated_model class decorator to efficiently
        return paginated sets on the appengine datastore
    """

    def __init__(self, object_list, per_page, readahead=10, **kwargs):
        if not object_list.ordered:
            object_list.order_by("pk") # Just order by PK by default

        self.original_orderings = object_list.query.order_by
        self.field_required = _field_name_for_ordering(self.original_orderings[:])
        self.readahead = readahead

        try:
            object_list.model._meta.get_field(self.field_required)
        except models.FieldDoesNotExist:
            raise PaginationOrderingRequired("No pagination ordering specified for {}".format(self.original_orderings))

        # Wipe out the existing ordering
        object_list = object_list.order_by()

        # Add our replacement ordering
        # A single negated ordering can use the same field (we just flip the query), this
        # normalisation happens in _field_name_for_ordering, so we do the same here.
        if len(self.original_orderings) == 1 and self.original_orderings[0].startswith("-"):
            object_list = object_list.order_by("-" + self.field_required)
        else:
            object_list = object_list.order_by(self.field_required)

        super(Paginator, self).__init__(object_list, per_page, **kwargs)

    @property
    def count(self):
        return _get_known_count(queryset_identifier(self.object_list))

    def validate_number(self, number):
        """
        Validates the given 1-based page number.
        """
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise paginator.PageNotAnInteger('That page number is not an integer')
        if number < 1:
            raise paginator.EmptyPage('That page number is less than 1')

        return number

    def page(self, number):
        """
        Returns a Page object for the given 1-based page number.
        """
        number = self.validate_number(number)
        bottom = (number - 1) * self.per_page
        top = bottom + self.per_page
        queryset_id = queryset_identifier(self.object_list)

        marker_value, pages = _get_marker(
            queryset_id,
            number
        )

        if marker_value:
            if len(self.original_orderings) == 1 and self.original_orderings[0].startswith("-"):
                qs = self.object_list.all().filter(**{"{}__lt".format(self.field_required): marker_value})
            else:
                qs = self.object_list.all().filter(**{"{}__gt".format(self.field_required): marker_value})
            bottom = pages * self.per_page # We have to skip the pages here
            top = bottom + self.per_page
        else:
            qs = self.object_list

        results = list(qs[bottom:top + (self.per_page * self.readahead)])

        next_page = results[top:]
        next_page_counter = number + 1
        while next_page:
            _store_marker(
                queryset_id,
                next_page_counter,
                getattr(next_page[self.per_page-1], self.field_required)
            )
            next_page_counter += 1
            next_page = next_page[self.per_page:]

        if not results:
            raise paginator.EmptyPage("That page contains no results")

        known_count = ((number - 1) * self.per_page) + len(results)
        _update_known_count(queryset_id, known_count)

        page = self._get_page(results[:top], number, self)

        _store_marker(
            queryset_id,
            number,
            getattr(page.object_list[self.per_page-1], self.field_required)
        )

        return page
