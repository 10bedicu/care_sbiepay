from rest_framework.permissions import BasePermission


class IsSuperUserOrReadOnly(BasePermission):
    """Authenticated users may read; only superusers may create/update merchants."""

    def has_permission(self, request, view):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return bool(request.user and request.user.is_authenticated)
        return bool(
            request.user and request.user.is_authenticated and request.user.is_superuser
        )
