from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Location, Organization, Role, User

admin.site.register(User, UserAdmin)
admin.site.register(Organization)
admin.site.register(Location)
admin.site.register(Role)
