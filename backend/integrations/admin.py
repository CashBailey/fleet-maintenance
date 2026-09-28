from django.contrib import admin

from .models import Device, DeviceAssetAssociation, NormalizedTelematicsEvent, TelematicsMessage

admin.site.register(Device)
admin.site.register(DeviceAssetAssociation)
admin.site.register(TelematicsMessage)
admin.site.register(NormalizedTelematicsEvent)
