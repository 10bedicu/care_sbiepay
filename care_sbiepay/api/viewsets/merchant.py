from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework.mixins import CreateModelMixin, RetrieveModelMixin, UpdateModelMixin
from rest_framework.viewsets import GenericViewSet

from care.emr.models.organization import FacilityOrganizationUser, OrganizationUser
from care.facility.models import Facility
from care_sbiepay.api.permissions import IsSuperUserOrReadOnly
from care_sbiepay.api.serializers.merchant import SbiEpayMerchantSerializer
from care_sbiepay.models import SbiEpayMerchant


@extend_schema(tags=["SBI ePay"])
class MerchantViewSet(
    GenericViewSet,
    CreateModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
):
    permission_classes = (IsSuperUserOrReadOnly,)
    queryset = SbiEpayMerchant.objects.all()
    serializer_class = SbiEpayMerchantSerializer
    lookup_field = "facility__external_id"

    def get_facility_queryset(self):
        qs = Facility.objects.all()
        if self.request.user.is_superuser:
            return qs

        organization_ids = list(
            OrganizationUser.objects.filter(user=self.request.user).values_list(
                "organization_id", flat=True
            )
        )
        return qs.filter(
            Q(
                id__in=FacilityOrganizationUser.objects.filter(
                    user=self.request.user
                ).values_list("organization__facility_id")
            )
            | Q(geo_organization_cache__overlap=organization_ids)
        )

    def get_queryset(self):
        return self.queryset.filter(facility__in=self.get_facility_queryset())
