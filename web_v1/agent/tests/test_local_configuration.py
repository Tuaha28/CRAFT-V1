from app.config import Settings


def test_local_profile_preserves_local_identity_and_reference_data():
    from app.auth import verify
    settings = Settings(environment='local', auth_mode='local', mode='offline',
                        tokens={'local-customer-1': 'CUST-001'})
    settings.validate()
    assert settings.environment == 'demo'
    assert settings.auth_mode == 'demo'
    identity = verify(settings, 'local-customer-1')
    assert identity.subject == 'CUST-001'
    assert 'customer:write' in identity.scopes
