from rest_framework import serializers

from care.facility.models import Facility
from care_sbiepay.models import SbiEpayMerchant

MASK_VISIBLE_CHARS = 4


class SbiEpayMerchantSerializer(serializers.ModelSerializer):
    id = serializers.UUIDField(source="external_id", read_only=True)
    facility_id = serializers.UUIDField()
    merchant_code = serializers.CharField(max_length=255)
    merchant_key = serializers.CharField(max_length=255, write_only=True)
    merchant_key_masked = serializers.SerializerMethodField()
    is_enabled = serializers.BooleanField(default=True)
    created_date = serializers.DateTimeField(read_only=True)
    modified_date = serializers.DateTimeField(read_only=True)

    class Meta:
        model = SbiEpayMerchant
        exclude = ("deleted", "facility", "external_id")

    def get_merchant_key_masked(self, instance) -> str:
        key = instance.merchant_key or ""
        return "*" * max(len(key) - MASK_VISIBLE_CHARS, 0) + key[-MASK_VISIBLE_CHARS:]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["facility_id"] = instance.facility.external_id
        return data

    def validate_facility_id(self, value):
        if self.instance and value != self.instance.facility.external_id:
            raise serializers.ValidationError("Facility cannot be changed.")
        if not Facility.objects.filter(external_id=value).exists():
            msg = f"Facility with external_id {value} does not exist."
            raise serializers.ValidationError(msg)
        return value

    def create(self, validated_data):
        facility_id = validated_data.pop("facility_id")
        validated_data["facility"] = Facility.objects.get(external_id=facility_id)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop("facility_id", None)
        return super().update(instance, validated_data)
