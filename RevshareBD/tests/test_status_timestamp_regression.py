from revshare.extract import BaseRow
from revshare.web import create_app


def test_status_dates_only_change_for_submission_or_real_status_change(tmp_path):
    store = create_app(tmp_path / 'test.db').state.store
    first = store.partner_by_token('demo-partner')['id']
    second = store.add_partner(name='Second', legal_name='', legal_country='',
        altegio_legal_entity='', contact_name='', email='second@example.com', telegram='',
        report_channel='email', token='second-token', rate=0.3, currency='USD')
    def submit(partner, locations):
        return store.submit_report(partner_id=partner, period='2026-08', turnover=100,
            currency='USD', reported_share=30, location_ids=locations, comment='', attachment=None)[0]
    first_report = submit(first, ['101'])
    old = '2020-01-01T00:00:00+00:00'
    with store.connect() as db:
        db.execute('UPDATE reports SET status_updated_at = ? WHERE id = ?', (old, first_report))
    second_report = submit(second, ['102'])
    assert store.report(first_report)['status_updated_at'] == old
    assert store.report(second_report)['status_updated_at'] != old
    store.replace_usage(first, '2026-08', connected_ids=[], active_ids=[])
    assert store.report(first_report)['status_updated_at'] == old
    submit(first, ['101'])
    assert store.report(first_report)['status_updated_at'] != old


def test_conflict_does_not_change_other_partner_status(tmp_path):
    store = create_app(tmp_path / 'test.db').state.store
    first = store.partner_by_token('demo-partner')['id']
    second = store.add_partner(name='Second', legal_name='', legal_country='',
        altegio_legal_entity='', contact_name='', email='second@example.com', telegram='',
        report_channel='email', token='second-token', rate=0.3, currency='USD')
    store.replace_reconciliation_base(period='2026-08', uploaded_at='2026-09-01T00:00:00+00:00',
        uploaded_by='test', file_path='test.csv', notes='', rows=[BaseRow(application_id=1,
        location_id='101', turnover=0, currency='USD', active=1, source_row=1)])
    def submit(partner):
        return store.submit_report(partner_id=partner, period='2026-08', turnover=100,
            currency='USD', reported_share=30, location_ids=['101'], comment='', attachment=None)[0]
    first_report = submit(first)
    old = '2020-01-01T00:00:00+00:00'
    with store.connect() as db:
        db.execute('UPDATE reports SET status_updated_at = ? WHERE id = ?', (old, first_report))
    submit(second)
    assert store.report(first_report)['status_updated_at'] == old


def test_submission_does_not_erase_other_partner_cross_partner_reason(tmp_path):
    store = create_app(tmp_path / 'test.db').state.store
    first = store.partner_by_token('demo-partner')['id']
    second = store.add_partner(name='Second', legal_name='', legal_country='',
        altegio_legal_entity='', contact_name='', email='second@example.com', telegram='',
        report_channel='email', token='second-token', rate=0.3, currency='USD')
    third = store.add_partner(name='Third', legal_name='', legal_country='',
        altegio_legal_entity='', contact_name='', email='third@example.com', telegram='',
        report_channel='email', token='third-token', rate=0.3, currency='USD')

    def submit(partner, locations):
        return store.submit_report(partner_id=partner, period='2026-08', turnover=100,
            currency='USD', reported_share=30, location_ids=locations, comment='', attachment=None)[0]

    submit(first, ['101'])
    second_report = submit(second, ['101'])
    before = store.report(second_report)
    assert any(reason['key'] == 'reason_cross_partner' for reason in before['reasons'])
    before_score = before['risk_score']

    submit(third, ['202'])
    after = store.report(second_report)
    assert any(reason['key'] == 'reason_cross_partner' for reason in after['reasons'])
    assert after['risk_score'] == before_score
