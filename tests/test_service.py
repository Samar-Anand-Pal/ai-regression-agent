"""The demo service: documented behavior on --fixed, planted bugs otherwise."""
import grpc
import pytest

from sample_service.client import ShelfwiseClient
from sample_service.server import start_server


@pytest.fixture(params=[True, False], ids=["fixed", "buggy"])
def client(request):
    srv, port, core = start_server(0, fixed=request.param)
    c = ShelfwiseClient(f"127.0.0.1:{port}")
    c.fixed, c.core = request.param, core
    yield c
    srv.stop(0)


def code(fn, *a):
    try:
        fn(*a)
        return "OK"
    except grpc.RpcError as e:
        return e.code().name


def test_fine_and_renewals(client):
    b = client.create_book("Dune", "Herbert", "1", 1)
    m = client.register_member("A", "a@x.io")
    loan = client.checkout_book(b.book_id, m.member_id)
    assert loan.due_day == 14
    assert code(client.renew_loan, loan.loan_id) == "OK" and code(client.renew_loan, loan.loan_id) == "OK"
    assert code(client.renew_loan, loan.loan_id) == ("FAILED_PRECONDITION" if client.fixed else "OK")
    client.core.s.today = client.get_loan(loan.loan_id).due_day + 4
    ret = client.return_book(loan.loan_id)
    assert ret.fine_cents == (100 if client.fixed else 75)
    assert client.get_account(m.member_id).balance_cents == ret.fine_cents


def test_hold_reservation(client):
    b = client.create_book("Dune", "Herbert", "1", 1)
    a, w, x = (client.register_member(n, f"{n}@x.io") for n in "awx")
    loan = client.checkout_book(b.book_id, a.member_id)
    hold = client.place_hold(b.book_id, w.member_id)
    client.return_book(loan.loan_id)
    assert client.list_holds(b.book_id).holds[0].status == client.pb2.HOLD_STATUS_READY
    assert code(client.checkout_book, b.book_id, x.member_id) == ("FAILED_PRECONDITION" if client.fixed else "OK")
    if client.fixed:
        client.checkout_book(b.book_id, w.member_id)
        assert client.list_holds(b.book_id).holds == []
        assert code(client.cancel_hold, hold.hold_id) == "FAILED_PRECONDITION"


def test_suspension(client):
    b = client.create_book("Dune", "Herbert", "1", 1)
    m = client.register_member("A", "a@x.io")
    client.suspend_member(m.member_id)
    assert code(client.checkout_book, b.book_id, m.member_id) == ("PERMISSION_DENIED" if client.fixed else "OK")
