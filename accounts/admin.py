# Registered only when Django admin is enabled (local development, decision D9).
from django.contrib import admin

from accounts.models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "is_active", "date_joined")
    search_fields = ("email",)
    readonly_fields = ("password", "last_login", "date_joined")
