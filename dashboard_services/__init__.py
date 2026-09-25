"""In-process dashboard services.

Pagination, conditional refresh, and the list envelope live here so the
HTTP handler stays a router. They are not separate network processes:
the dashboard is one trusted-LAN server, and an extra hop would make
the polls this package exists to shrink slower.
"""

from dashboard_services.page import apply as page_apply
from dashboard_services.page import requested as page_requested
from dashboard_services.refresh import etag_for, not_modified

__all__ = ["etag_for", "not_modified", "page_apply", "page_requested"]
