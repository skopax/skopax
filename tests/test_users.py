import hashlib
import itertools

import mongomock
import pytest
from pymongo.errors import AutoReconnect, DuplicateKeyError

from skopax import users as users_mod
from skopax.messages import msg
from skopax.users import InvalidUser, LastAdmin, UserExists, Users, admin_set_of, check_password, hash_password

PW = 'correct horse battery'


@pytest.fixture
def users() -> Users:
    u = Users(mongomock.MongoClient(tz_aware=True)['skopax'])
    u.ensure_indexes()
    return u


def test_hash_round_trip_and_format():
    # given
    stored = hash_password(PW)
    # then
    assert stored.startswith('scrypt$16384$8$1$')
    assert check_password(PW, stored) is True
    assert check_password('wrong password here', stored) is False
    assert hash_password(PW) != stored


def test_malformed_hash_is_false():
    assert check_password('x' * 12, 'nope') is False
    assert check_password('x' * 12, 'scrypt$a$b$c$d$e') is False


def test_an_unusable_stored_hash_still_pays_one_pinned_scrypt(monkeypatch):
    # given
    calls = []
    real = hashlib.scrypt
    salt, digest = hash_password(PW).split('$')[4:]
    unpinned = f'scrypt$32768$8$1${salt}${digest}'
    monkeypatch.setattr(users_mod.hashlib, 'scrypt', lambda *a, **kw: calls.append(kw['n']) or real(*a, **kw))

    # when
    seen = [check_password(PW, stored) for stored in (unpinned, f'bcrypt$16384$8$1${salt}${digest}', 'nope', 'scrypt$a$b$c$d$e')]

    # then
    assert seen == [False] * 4
    assert calls == [users_mod.SCRYPT_N] * 4, 'every unusable hash runs one scrypt at the pinned cost, like an unknown user'


def test_a_lone_surrogate_password_is_invalid_not_an_encoding_crash(users):
    # given
    lone = 'long enough ' + chr(0xd800) + ' pass'

    # when
    with pytest.raises(InvalidUser) as refused:
        users.create('erin@example.com', 'Erin', 'viewer', lone)
    with pytest.raises(InvalidUser):
        users.create('erin@example.com', 'Erin', 'viewer', 'long enough ' + chr(0xdcff) + ' pass')

    # then
    assert str(refused.value) == msg('user.password_text')
    assert users.get('erin@example.com') is None


def test_create_get_and_case_folding(users):
    # when
    made = users.create('Alice@Example.com', 'Alice', 'viewer', PW)
    # then
    assert made['_id'] == 'alice@example.com'
    assert 'pw' not in made
    assert users.get('ALICE@example.com')['name'] == 'Alice'
    assert 'pw' not in users.get('alice@example.com')
    assert all('pw' not in row for row in users.list())
    assert users.count() == 1


def test_duplicate_and_invalid(users):
    # given
    users.create('alice@example.com', 'Alice', 'viewer', PW)
    # then
    with pytest.raises(UserExists):
        users.create('ALICE@example.com', 'Alice', 'viewer', PW)
    with pytest.raises(InvalidUser):
        users.create('not-an-email', 'Bob', 'viewer', PW)
    with pytest.raises(InvalidUser):
        users.create('bob@example.com', 'Bob', 'viewer', 'x' * 11)
    with pytest.raises(InvalidUser):
        users.create('bob@example.com', 'Bob', 'root', PW)
    with pytest.raises(InvalidUser):
        users.create('bob@example.com', '', 'viewer', PW)


def test_authenticate_rules(users):
    # given
    users.create('alice@example.com', 'Alice', 'operator', PW)
    users.create('bob@example.com', 'Bob', 'viewer', PW)
    users.upsert_sso('carol@example.com', 'Carol', 'viewer')
    users.set_disabled('bob@example.com', True)
    # then
    assert users.authenticate('Alice@example.com', PW)['role'] == 'operator'
    assert 'pw' not in users.authenticate('alice@example.com', PW)
    assert users.authenticate('alice@example.com', 'wrong password!') is None
    assert users.authenticate('bob@example.com', PW) is None
    assert users.authenticate('carol@example.com', PW) is None


def test_unknown_user_still_pays_scrypt(users, monkeypatch):
    # given
    calls = []
    real = hashlib.scrypt

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(users_mod.hashlib, 'scrypt', counting)
    # when
    result = users.authenticate('nobody@example.com', 'x' * 12)
    # then
    assert result is None
    assert len(calls) == 1


def test_upsert_sso_keeps_role(users):
    # when
    first = users.upsert_sso('carol@example.com', 'Carol', 'viewer')
    users.set_role('carol@example.com', 'admin')
    second = users.upsert_sso('carol@example.com', 'Carol B', 'viewer')
    # then
    assert first['role'] == 'viewer'
    assert first['source'] == 'sso'
    assert second['role'] == 'admin'
    assert second['name'] == 'Carol B'
    assert second['lastLogin']


def test_active_admins_counts_enabled_admins_only(users):
    # given
    users.create('alice@example.com', 'Alice', 'admin', PW)
    users.create('bob@example.com', 'Bob', 'admin', PW)
    users.create('carol@example.com', 'Carol', 'operator', PW)
    # when
    users.set_disabled('bob@example.com', True)
    # then
    assert users.active_admins() == 1
    assert users.count() == 3
    assert users.count(active_only=True) == 2
    assert [u['email'] for u in users.list()] == ['alice@example.com', 'bob@example.com', 'carol@example.com']


def test_set_password_and_touch_login(users):
    # given
    users.create('alice@example.com', 'Alice', 'viewer', PW)
    # when
    users.set_password('alice@example.com', 'another long password')
    users.touch_login('alice@example.com')
    # then
    assert users.authenticate('alice@example.com', PW) is None
    assert users.authenticate('alice@example.com', 'another long password')['lastLogin']
    with pytest.raises(InvalidUser):
        users.set_password('alice@example.com', 'short')


def test_sign_in_role_records_its_source_and_keeps_the_admin_set_role(users):
    # given
    users.create('ann@example.com', 'Ann', 'operator', source='sso')
    users.upsert_sso('ben@example.com', 'Ben', 'viewer')
    users.create('cat@example.com', 'Cat', 'admin')

    # when
    legacy, fresh = admin_set_of(users.get('ann@example.com')), admin_set_of(users.get('ben@example.com'))
    promoted = users.apply_sign_in_role('ann@example.com', 'admin', 'claim')
    demoted = users.apply_sign_in_role('ann@example.com', admin_set_of(promoted), 'admin')
    ben = users.apply_sign_in_role('ben@example.com', 'operator', 'resolver')

    # then
    assert (legacy, fresh) == ('operator', None), 'a role without roleFrom counts as Admin-set; an upserted default does not'
    assert (promoted['role'], promoted['roleFrom'], promoted['adminRole']) == ('admin', 'claim', 'operator')
    assert (demoted['role'], demoted['roleFrom'], demoted['adminRole']) == ('operator', 'admin', 'operator')
    assert (ben['role'], ben['roleFrom'], 'adminRole' in ben, admin_set_of(ben)) == ('operator', 'resolver', False, None)
    with pytest.raises(ValueError):
        users.apply_sign_in_role('ann@example.com', 'admin', 'plugin')
    users.create('dee@example.com', 'Dee', 'operator')
    local = users.apply_sign_in_role('dee@example.com', 'viewer', 'claim')
    assert (local['role'], 'roleFrom' in local, 'adminRole' in local) == ('operator', False, False), 'a local account is never written by a sign-in'
    with pytest.raises(TypeError):
        users.prepare_change('viewer', None, None)


def test_sso_user_never_authenticates_locally_even_with_a_stray_hash(users, monkeypatch):
    # given
    users.upsert_sso('carol@example.com', 'Carol', 'viewer')
    users.col.update_one({'_id': 'carol@example.com'}, {'$set': {'pw': hash_password(PW)}})
    calls = []
    real = hashlib.scrypt

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)

    monkeypatch.setattr(users_mod.hashlib, 'scrypt', counting)
    # when
    result = users.authenticate('carol@example.com', PW)
    # then
    assert result is None
    assert len(calls) == 1


def test_a_claim_demotion_never_overwrites_a_role_an_admin_set_meanwhile(users):
    # given
    users.create('admin@example.com', 'Admin', 'admin')
    users.upsert_sso('ivy@example.com', 'Ivy', 'viewer')
    users.apply_sign_in_role('ivy@example.com', 'admin', 'claim')
    seen = users.get('ivy@example.com')
    users.set_role('ivy@example.com', 'operator')

    # when
    after = users.apply_sign_in_role('ivy@example.com', 'viewer', 'default', seen=seen)

    # then
    assert (after['role'], after['roleFrom'], after['adminRole']) == ('operator', 'admin', 'operator'), 'the Admin decision stands; the claim no longer owns the role'


def test_a_failed_rollback_of_the_last_admin_check_is_logged_and_the_guard_error_still_surfaces(users, monkeypatch, caplog):
    # given
    users.create('admin@example.com', 'Admin', 'admin')
    real = users.col.update_one

    def down(*args, **kwargs):
        raise AutoReconnect('mongo went away')

    monkeypatch.setattr(users.col, 'update_one', down)

    # when
    with pytest.raises(LastAdmin):
        users.guarded_update('admin@example.com', {'role': 'viewer'})

    # then
    monkeypatch.setattr(users.col, 'update_one', real)
    assert [r.levelname for r in caplog.records if 'could not be restored' in r.getMessage()] == ['ERROR']
    assert users.get('admin@example.com')['role'] == 'viewer', 'the failed restore is reported, not hidden'


def test_the_rollback_of_the_last_admin_check_skips_fields_changed_since_it_wrote_them(users, monkeypatch):
    # given
    users.create('admin@example.com', 'Admin', 'admin')
    real = Users.active_admins

    def someone_sets_the_role_meanwhile(self):
        self.col.update_one({'_id': 'admin@example.com'}, {'$set': {'role': 'operator'}})
        return real(self)

    monkeypatch.setattr(Users, 'active_admins', someone_sets_the_role_meanwhile)

    # when
    with pytest.raises(LastAdmin):
        users.guarded_update('admin@example.com', {'role': 'viewer'})

    # then
    assert users.get('admin@example.com')['role'] == 'operator', 'the later write stands; the restore did not overwrite it'


def test_the_rollback_of_the_last_admin_check_restores_what_it_wrote_when_nothing_changed_meanwhile(users):
    # given
    users.create('admin@example.com', 'Admin', 'admin')

    # when
    with pytest.raises(LastAdmin):
        users.guarded_update('admin@example.com', {'role': 'viewer', 'disabled': True}, ('roleFrom',))

    # then
    after = users.get('admin@example.com')
    assert after['role'] == 'admin' and after['disabled'] is False


def test_a_claim_promotion_never_marks_a_role_an_admin_set_meanwhile(users, monkeypatch):
    # given
    users.create('admin@example.com', 'Admin', 'admin')
    users.create('ivy@example.com', 'Ivy', 'viewer', source='sso')
    real = users.col.find_one
    fired = []

    def admin_promotes_meanwhile(*a, **kw):
        found = real(*a, **kw)
        if not fired:
            fired.append(1)
            users.set_role('ivy@example.com', 'admin')
        return found

    monkeypatch.setattr(users.col, 'find_one', admin_promotes_meanwhile)

    # when
    users.apply_sign_in_role('ivy@example.com', 'operator', 'claim')

    # then
    stored = real({'_id': 'ivy@example.com'})
    assert (stored['role'], stored['roleFrom'], stored['adminRole']) == ('admin', 'admin', 'admin'), 'the Admin-set admin is kept, so a later login without the claim gets it back'


def test_a_concurrent_first_sso_sign_in_is_not_called_a_local_account(users: Users, monkeypatch):
    # given
    ticks = itertools.count()
    monkeypatch.setattr(users_mod, 'stamp', lambda: f'2026-11-17T10:00:{5 * next(ticks):02d}Z')
    first = users.upsert_sso('carol@example.com', 'Carol', 'viewer')['lastLogin']
    users.create('alice@example.com', 'Alice', 'viewer', PW)
    real = users.col.update_one
    raised = []

    def racing(*args, **kwargs):
        if kwargs.get('upsert') and not raised:
            raised.append(args[0])
            raise DuplicateKeyError('E11000 duplicate key error')
        return real(*args, **kwargs)

    monkeypatch.setattr(users.col, 'update_one', racing)

    # when
    second = users.upsert_sso('carol@example.com', 'Carol Stone', 'operator')
    raised.clear()
    with pytest.raises(UserExists):
        users.upsert_sso('alice@example.com', 'Alice', 'viewer')
    local = users.get('alice@example.com')

    # then
    assert second is not None and second['source'] == 'sso' and not second.get('disabled')
    assert (second['name'], second['role']) == ('Carol Stone', 'viewer')
    assert second['lastLogin'] > first, 'the retry records this sign-in, not the first one'
    assert (local['source'], local['name'], local.get('lastLogin')) == ('local', 'Alice', None)
