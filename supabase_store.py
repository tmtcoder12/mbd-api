"""Backward-compatible exports for the Supabase repository."""

from mbd_api.repository import SupabaseStore, SupabaseStoreError

__all__ = ["SupabaseStore", "SupabaseStoreError"]
