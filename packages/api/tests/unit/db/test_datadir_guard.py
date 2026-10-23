"""The ownership and access rules for the bundled database's folders (Spec WIN review M-1).

The rules are pure functions over an owner and an access list, so each case is built
here as a test double: in particular a folder OWNED BY ANOTHER ACCOUNT, which this
single-account PC cannot create for real. The real reads and writes are proved by
``test_managed_files_windows.py`` on Windows.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from persona_api.db._datadir_guard import (
    base_folder_refusal,
    datadir_refusal,
    private_file_refusal,
    restricted_datadir_refusal,
)

ME = "S-1-5-21-1-2-3-1001"
OTHER = "S-1-5-21-1-2-3-1002"
SYSTEM = "S-1-5-18"
ADMINS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
OWNER_RIGHTS = "S-1-3-4"
FULL = 0x001F01FF
READ = 0x00120089
MODIFY = 0x001301BF
FOLDER = Path("C:/data/persona")


@dataclass(frozen=True)
class Entry:
    sid: str
    allow: bool = True
    mask: int = FULL
    inherited: bool = False
    inherit_only: bool = False


@dataclass(frozen=True)
class Summary:
    owner: str
    entries: tuple[Entry, ...] | None
    unknown_entries: int = 0


OWNER_ONLY = (Entry(ME), Entry(SYSTEM))


class TestOwner:
    @pytest.mark.parametrize("rule", [base_folder_refusal, datadir_refusal, private_file_refusal])
    def test_another_account_owning_it_is_refused(self, rule: object) -> None:
        refusal = rule(Summary(OTHER, OWNER_ONLY), ME, FOLDER)  # type: ignore[operator]
        assert refusal is not None
        assert OTHER in refusal
        assert str(FOLDER) in refusal

    @pytest.mark.parametrize("owner", [ME, SYSTEM, ADMINS])
    @pytest.mark.parametrize("rule", [base_folder_refusal, datadir_refusal, private_file_refusal])
    def test_this_user_system_and_administrators_are_trusted_owners(
        self, rule: object, owner: str
    ) -> None:
        assert rule(Summary(owner, OWNER_ONLY), ME, FOLDER) is None  # type: ignore[operator]

    @pytest.mark.parametrize(
        "rule",
        [base_folder_refusal, datadir_refusal, private_file_refusal, restricted_datadir_refusal],
    )
    def test_a_null_dacl_or_an_unreadable_entry_is_refused(self, rule: object) -> None:
        assert rule(Summary(ME, None), ME, FOLDER) is not None  # type: ignore[operator]
        assert rule(Summary(ME, OWNER_ONLY, 1), ME, FOLDER) is not None  # type: ignore[operator]


class TestBaseFolder:
    def test_the_default_profile_folder_passes(self) -> None:
        entries = (Entry(SYSTEM), Entry(ADMINS), Entry(ME), Entry(OWNER_RIGHTS))
        assert base_folder_refusal(Summary(ME, entries), ME, FOLDER) is None

    def test_another_account_that_can_add_folders_is_refused(self) -> None:
        """The C:\\ case: Authenticated Users may create folders, so pg16 could be planted."""
        entries = (*OWNER_ONLY, Entry("S-1-5-11", mask=MODIFY, inherited=True))
        refusal = base_folder_refusal(Summary(ME, entries), ME, FOLDER)
        assert refusal is not None
        assert "PERSONA_API_COMMUNITY_MANAGED_DB_DIR" in refusal

    def test_another_account_that_can_only_read_is_fine(self) -> None:
        entries = (*OWNER_ONLY, Entry(USERS, mask=READ))
        assert base_folder_refusal(Summary(ME, entries), ME, FOLDER) is None

    def test_an_entry_that_applies_only_to_children_is_ignored(self) -> None:
        entries = (*OWNER_ONLY, Entry(USERS, mask=FULL, inherit_only=True))
        assert base_folder_refusal(Summary(ME, entries), ME, FOLDER) is None

    def test_a_deny_entry_is_never_a_grant(self) -> None:
        entries = (*OWNER_ONLY, Entry(USERS, allow=False, mask=FULL))
        assert base_folder_refusal(Summary(ME, entries), ME, FOLDER) is None


class TestDataFolder:
    def test_an_explicit_grant_to_another_account_is_refused(self) -> None:
        entries = (*OWNER_ONLY, Entry(USERS, mask=READ))
        refusal = datadir_refusal(Summary(ME, entries), ME, FOLDER)
        assert refusal is not None
        assert USERS in refusal

    def test_an_older_builds_inherited_entries_are_accepted_for_replacement(self) -> None:
        """A pgserver (trust era) data folder inherits SYSTEM, Administrators and OWNER RIGHTS."""
        entries = (
            Entry(SYSTEM, inherited=True),
            Entry(ADMINS, inherited=True),
            Entry(OWNER_RIGHTS, inherited=True),
        )
        assert datadir_refusal(Summary(ME, entries), ME, FOLDER) is None

    def test_after_the_owner_only_dacl_only_this_user_and_system_remain(self) -> None:
        assert restricted_datadir_refusal(Summary(ME, OWNER_ONLY), ME, FOLDER) is None
        leftover = (*OWNER_ONLY, Entry(ADMINS, inherited=True))
        assert restricted_datadir_refusal(Summary(ME, leftover), ME, FOLDER) is not None
        inherited_me = (Entry(ME, inherited=True), Entry(SYSTEM))
        assert restricted_datadir_refusal(Summary(ME, inherited_me), ME, FOLDER) is not None


class TestPrivateFile:
    def test_owner_only_passes(self) -> None:
        assert private_file_refusal(Summary(ME, OWNER_ONLY), ME, FOLDER) is None

    @pytest.mark.parametrize("inherited", [False, True])
    def test_any_other_account_with_access_is_refused(self, inherited: bool) -> None:
        entries = (*OWNER_ONLY, Entry(ADMINS, mask=READ, inherited=inherited))
        refusal = private_file_refusal(Summary(ME, entries), ME, FOLDER)
        assert refusal is not None
        assert ADMINS in refusal
