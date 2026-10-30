import threading
import time
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.db import connection
from django.test import Client, override_settings
from django.utils.timezone import now
from django_scopes import scopes_disabled

from eventyay.base.models import Event, Order, OrderPayment, OrderRefund, Organizer, Team, User
from eventyay.base.models.cache import VersionedModel


@pytest.fixture
def env(monkeypatch):
    # In a transactional test ``on_commit`` runs immediately, and the Redis cache hooks of
    # ``VersionedModel`` need an event loop that does not exist here.
    monkeypatch.setattr(VersionedModel, '_set_cache_version_sync', lambda self: None)
    monkeypatch.setattr(VersionedModel, '_set_cache_deleted_sync', lambda self: None)
    organizer = Organizer.objects.create(name='Dummy', slug='dummy')
    event = Event.objects.create(
        organizer=organizer,
        name='Dummy',
        slug='dummy',
        date_from=now(),
        plugins='eventyay.plugins.banktransfer',
    )
    user = User.objects.create_user('dummy@dummy.dummy', 'dummy')
    team = Team.objects.create(organizer=organizer, can_view_orders=True, can_change_orders=True)
    team.members.add(user)
    team.limit_events.add(event)
    order = Order.objects.create(
        code='FOO',
        event=event,
        email='dummy@dummy.test',
        status=Order.STATUS_PAID,
        datetime=now(),
        expires=now() + timedelta(days=10),
        total=Decimal('14.00'),
        locale='en',
    )
    payment = order.payments.create(
        amount=order.total,
        provider='banktransfer',
        state=OrderPayment.PAYMENT_STATE_CONFIRMED,
        payment_date=now(),
    )
    return order, payment


# With DEBUG off, the control middleware redirects organizer pages to SITE_URL, which is not an allowed host here.
@override_settings(DEBUG=True)
@pytest.mark.django_db(transaction=True)
def test_concurrent_control_panel_refunds_exceed_payment_amount(env, monkeypatch):
    """
    Two organizers submit the control panel refund form for the full payment amount at the same time.
    ``OrderRefundView`` checks the refundable amount and creates the ``OrderRefund`` without locking
    the payment, so both requests can pass the check.
    Expected: the refunds together do not exceed the payment amount.
    """
    order, payment = env

    provider = SimpleNamespace(
        payment_refund_supported=lambda p: True,
        payment_partial_refund_supported=lambda p: True,
        execute_refund=lambda refund: None,
    )
    monkeypatch.setattr(OrderPayment, 'payment_provider', property(lambda self: provider))
    monkeypatch.setattr(OrderRefund, 'payment_provider', property(lambda self: provider))

    # Keep the request that has read the refunded amount in the check-then-create window, so the
    # two requests overlap reliably.
    original = OrderPayment.refunded_amount.fget

    def refunded_amount_then_sleep(self):
        value = original(self)
        time.sleep(1)
        return value

    monkeypatch.setattr(OrderPayment, 'refunded_amount', property(refunded_amount_then_sleep))

    start = threading.Barrier(2, timeout=10)
    results = []
    errors = []

    def fire():
        try:
            client = Client()
            assert client.login(email='dummy@dummy.dummy', password='dummy')
            start.wait()
            results.append(
                client.post(
                    '/control/event/dummy/dummy/orders/FOO/refund',
                    {
                        'start-mode': 'full',
                        'start-partial_amount': '14.00',
                        'start-action': 'do_nothing',
                        'refund-{}'.format(payment.pk): '14.00',
                        'perform': 'on',
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, 'A request thread failed to run: {!r}'.format(errors)
    with scopes_disabled():
        refunds = list(order.refunds.values_list('amount', flat=True))
    print('STATUS CODES:', [r.status_code for r in results])
    print('REFUNDS CREATED:', refunds, 'TOTAL:', sum(refunds), 'PAYMENT AMOUNT: 14.00')

    assert sum(refunds) <= Decimal('14.00'), 'Over-refund: refunded {} of a 14.00 payment'.format(sum(refunds))
