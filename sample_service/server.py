"""Shelfwise demo server (in-memory).

    python -m sample_service.server --port 50051            # with the planted bugs
    python -m sample_service.server --port 50051 --fixed    # behaves exactly as documented

The planted bugs are listed in PLANTED_BUGS.md. They give the regression agent
something real to find; each one breaks a single documented requirement.
"""
from __future__ import annotations

import argparse
import itertools
import threading
from concurrent import futures
from dataclasses import dataclass, field
from pathlib import Path

import grpc

from .protoc import compile_proto

DEFAULT_PROTO = Path(__file__).parent / "protos" / "shelfwise.proto"
FINE_PER_DAY = 25
MAX_LOANS = 5
MAX_RENEWALS = 2
MAX_BALANCE = 500


class Abort(Exception):
    def __init__(self, code: grpc.StatusCode, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class State:
    today: int = 0
    books: dict = field(default_factory=dict)
    members: dict = field(default_factory=dict)
    loans: dict = field(default_factory=dict)
    holds: dict = field(default_factory=dict)
    balances: dict = field(default_factory=dict)
    reserved: dict = field(default_factory=dict)  # book_id -> copies set aside for READY holds
    ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self.ids)}"


class Shelfwise:
    """All business rules live here; the gRPC servicers below only translate."""

    def __init__(self, pb2, fixed: bool = False, loan_days: int = 14):
        self.pb2, self.fixed, self.loan_days = pb2, fixed, loan_days
        self.lock = threading.RLock()
        self.s = State()

    # ------------------------------------------------------------ helpers
    def _book(self, book_id):
        if book_id not in self.s.books:
            raise Abort(grpc.StatusCode.NOT_FOUND, f"book {book_id!r} not found")
        return self.s.books[book_id]

    def _member(self, member_id):
        if member_id not in self.s.members:
            raise Abort(grpc.StatusCode.NOT_FOUND, f"member {member_id!r} not found")
        return self.s.members[member_id]

    def _loan(self, loan_id):
        if loan_id not in self.s.loans:
            raise Abort(grpc.StatusCode.NOT_FOUND, f"loan {loan_id!r} not found")
        return self.s.loans[loan_id]

    def _queue(self, book_id):
        """Active holds (WAITING or READY) for a book, in queue order."""
        P = self.pb2
        q = [h for h in self.s.holds.values() if h.book_id == book_id
             and h.status in (P.HOLD_STATUS_WAITING, P.HOLD_STATUS_READY)]
        return sorted(q, key=lambda h: int(h.hold_id.split("-")[1]))

    def _renumber(self, book_id):
        for i, h in enumerate(self._queue(book_id), start=1):
            h.position = i
        for h in self.s.holds.values():
            if h.book_id == book_id and h.status not in (self.pb2.HOLD_STATUS_WAITING, self.pb2.HOLD_STATUS_READY):
                h.position = 0

    def _release_copy(self, book):
        """A copy came back (return or cancelled READY hold): give it to the queue or the shelf."""
        P = self.pb2
        waiting = [h for h in self._queue(book.book_id) if h.status == P.HOLD_STATUS_WAITING]
        if waiting:
            waiting[0].status = P.HOLD_STATUS_READY
            if self.fixed:
                self.s.reserved[book.book_id] = self.s.reserved.get(book.book_id, 0) + 1
            else:
                # PLANTED BUG (REQ-032/REQ-033): the copy is not set aside for the
                # READY hold; it goes back on the shelf where anyone can take it.
                book.available_copies += 1
        else:
            book.available_copies += 1

    # ------------------------------------------------------------ catalog
    def create_book(self, r):
        if not r.title.strip() or not r.isbn.strip():
            raise Abort(grpc.StatusCode.INVALID_ARGUMENT, "title and isbn are required")
        if not 1 <= r.total_copies <= 50:
            raise Abort(grpc.StatusCode.INVALID_ARGUMENT, "total_copies must be between 1 and 50")
        if any(b.isbn == r.isbn for b in self.s.books.values()):
            raise Abort(grpc.StatusCode.ALREADY_EXISTS, f"isbn {r.isbn} already in catalog")
        b = self.pb2.Book(book_id=self.s.new_id("book"), title=r.title, author=r.author, isbn=r.isbn,
                          total_copies=r.total_copies, available_copies=r.total_copies)
        self.s.books[b.book_id] = b
        return b

    def get_book(self, r):
        return self._book(r.book_id)

    def list_books(self, r):
        books = [b for b in self.s.books.values() if not r.author or b.author == r.author]
        return self.pb2.ListBooksResponse(books=books)

    def update_book_copies(self, r):
        b = self._book(r.book_id)
        busy = b.total_copies - b.available_copies  # on loan or set aside
        if r.total_copies < busy:
            raise Abort(grpc.StatusCode.FAILED_PRECONDITION, "fewer copies than are currently on loan")
        if not 1 <= r.total_copies <= 50:
            raise Abort(grpc.StatusCode.INVALID_ARGUMENT, "total_copies must be between 1 and 50")
        b.available_copies += r.total_copies - b.total_copies
        b.total_copies = r.total_copies
        return b

    def delete_book(self, r):
        b = self._book(r.book_id)
        if b.available_copies < b.total_copies:
            raise Abort(grpc.StatusCode.FAILED_PRECONDITION, "copies are on loan")
        del self.s.books[b.book_id]
        return self.pb2.DeleteBookResponse(deleted=True)

    # ------------------------------------------------------------ members
    def register_member(self, r):
        if not r.name.strip() or "@" not in r.email:
            raise Abort(grpc.StatusCode.INVALID_ARGUMENT, "name and a valid email are required")
        if any(m.email.lower() == r.email.lower() for m in self.s.members.values()):
            raise Abort(grpc.StatusCode.ALREADY_EXISTS, "email already registered")
        m = self.pb2.Member(member_id=self.s.new_id("member"), name=r.name, email=r.email,
                            status=self.pb2.MEMBER_STATUS_ACTIVE)
        self.s.members[m.member_id] = m
        self.s.balances[m.member_id] = 0
        return m

    def get_member(self, r):
        return self._member(r.member_id)

    def suspend_member(self, r):
        m = self._member(r.member_id)
        m.status = self.pb2.MEMBER_STATUS_SUSPENDED
        return m

    def reinstate_member(self, r):
        m = self._member(r.member_id)
        m.status = self.pb2.MEMBER_STATUS_ACTIVE
        return m

    def update_member_email(self, r):  # v2 only
        m = self._member(r.member_id)
        if "@" not in r.email:
            raise Abort(grpc.StatusCode.INVALID_ARGUMENT, "a valid email is required")
        if any(o.email.lower() == r.email.lower() and o.member_id != m.member_id for o in self.s.members.values()):
            raise Abort(grpc.StatusCode.ALREADY_EXISTS, "email already registered")
        m.email = r.email
        return m

    # ------------------------------------------------------------ lending
    def checkout_book(self, r):
        P = self.pb2
        b, m = self._book(r.book_id), self._member(r.member_id)
        # PLANTED BUG (REQ-014): the fixed server refuses suspended members; the
        # buggy one forgets to check.
        if self.fixed and m.status == P.MEMBER_STATUS_SUSPENDED:
            raise Abort(grpc.StatusCode.PERMISSION_DENIED, "member is suspended")
        if self.s.balances.get(m.member_id, 0) > MAX_BALANCE:
            raise Abort(grpc.StatusCode.FAILED_PRECONDITION, "unpaid fines over the limit")
        active = [l for l in self.s.loans.values() if l.member_id == m.member_id and l.status == P.LOAN_STATUS_ACTIVE]
        if len(active) >= MAX_LOANS:
            raise Abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "loan limit reached")

        ready = next((h for h in self._queue(b.book_id)
                      if h.status == P.HOLD_STATUS_READY and h.member_id == m.member_id), None)
        if ready and self.s.reserved.get(b.book_id, 0) > 0:
            self.s.reserved[b.book_id] -= 1
        elif b.available_copies > 0:
            b.available_copies -= 1
        else:
            raise Abort(grpc.StatusCode.FAILED_PRECONDITION, "no copies available")
        if ready:
            ready.status = P.HOLD_STATUS_FULFILLED
            self._renumber(b.book_id)

        loan = P.Loan(loan_id=self.s.new_id("loan"), book_id=b.book_id, member_id=m.member_id,
                      status=P.LOAN_STATUS_ACTIVE, due_day=self.s.today + self.loan_days)
        self.s.loans[loan.loan_id] = loan
        return loan

    def return_book(self, r):
        P = self.pb2
        loan = self._loan(r.loan_id)
        if loan.status == P.LOAN_STATUS_RETURNED:
            raise Abort(grpc.StatusCode.FAILED_PRECONDITION, "loan already returned")
        late = self.s.today - loan.due_day
        if late > 0:
            # PLANTED BUG (REQ-024): off by one day on the buggy server.
            days = late if self.fixed else late - 1
            loan.fine_cents = FINE_PER_DAY * days
            self.s.balances[loan.member_id] = self.s.balances.get(loan.member_id, 0) + loan.fine_cents
        loan.status = P.LOAN_STATUS_RETURNED
        if loan.book_id in self.s.books:
            self._release_copy(self.s.books[loan.book_id])
        return loan

    def renew_loan(self, r):
        P = self.pb2
        loan = self._loan(r.loan_id)
        if loan.status != P.LOAN_STATUS_ACTIVE:
            raise Abort(grpc.StatusCode.FAILED_PRECONDITION, "only active loans can be renewed")
        # PLANTED BUG (REQ-025): the buggy server allows one renewal too many.
        limit = MAX_RENEWALS if self.fixed else MAX_RENEWALS + 1
        if loan.renewals >= limit:
            raise Abort(grpc.StatusCode.FAILED_PRECONDITION, "renewal limit reached")
        if any(h.member_id != loan.member_id for h in self._queue(loan.book_id)):
            raise Abort(grpc.StatusCode.FAILED_PRECONDITION, "another member is waiting for this book")
        loan.due_day += self.loan_days
        loan.renewals += 1
        return loan

    def get_loan(self, r):
        return self._loan(r.loan_id)

    def list_member_loans(self, r):
        self._member(r.member_id)
        loans = [l for l in self.s.loans.values() if l.member_id == r.member_id]
        return self.pb2.ListMemberLoansResponse(loans=loans)

    # ------------------------------------------------------------ holds
    def place_hold(self, r):
        P = self.pb2
        self._book(r.book_id)
        self._member(r.member_id)
        if any(h.member_id == r.member_id for h in self._queue(r.book_id)):
            raise Abort(grpc.StatusCode.ALREADY_EXISTS, "member already has a hold on this book")
        h = P.Hold(hold_id=self.s.new_id("hold"), book_id=r.book_id, member_id=r.member_id,
                   status=P.HOLD_STATUS_WAITING)
        self.s.holds[h.hold_id] = h
        self._renumber(r.book_id)
        return h

    def cancel_hold(self, r):
        P = self.pb2
        if r.hold_id not in self.s.holds:
            raise Abort(grpc.StatusCode.NOT_FOUND, f"hold {r.hold_id!r} not found")
        h = self.s.holds[r.hold_id]
        if h.status not in (P.HOLD_STATUS_WAITING, P.HOLD_STATUS_READY):
            raise Abort(grpc.StatusCode.FAILED_PRECONDITION, "hold is no longer active")
        was_ready = h.status == P.HOLD_STATUS_READY
        h.status = P.HOLD_STATUS_CANCELLED
        if was_ready and self.s.reserved.get(h.book_id, 0) > 0 and h.book_id in self.s.books:
            self.s.reserved[h.book_id] -= 1
            self._release_copy(self.s.books[h.book_id])
        self._renumber(h.book_id)
        return h

    def list_holds(self, r):
        self._book(r.book_id)
        return self.pb2.ListHoldsResponse(holds=self._queue(r.book_id))

    # ------------------------------------------------------------ billing
    def get_account(self, r):
        self._member(r.member_id)
        return self.pb2.Account(member_id=r.member_id, balance_cents=self.s.balances.get(r.member_id, 0))

    def pay_fine(self, r):
        self._member(r.member_id)
        bal = self.s.balances.get(r.member_id, 0)
        if r.amount_cents <= 0 or r.amount_cents > bal:
            raise Abort(grpc.StatusCode.INVALID_ARGUMENT, "amount must be positive and at most the balance")
        self.s.balances[r.member_id] = bal - r.amount_cents
        return self.pb2.Account(member_id=r.member_id, balance_cents=self.s.balances[r.member_id])

    # ------------------------------------------------------------ test support
    def advance_clock(self, r):
        if r.days <= 0:
            raise Abort(grpc.StatusCode.INVALID_ARGUMENT, "days must be positive")
        self.s.today += r.days
        return self.pb2.ClockResponse(today=self.s.today)

    def reset(self, r):
        self.s = State()
        return self.pb2.ResetResponse(ok=True)


def _servicer(pb2_grpc, service_name: str, core: Shelfwise):
    """Builds a servicer class whose every RPC calls the matching snake_case method on core."""
    base = getattr(pb2_grpc, f"{service_name}Servicer")
    methods = {}
    for rpc in [n for n in dir(base) if not n.startswith("_")]:
        impl_name = "".join("_" + c.lower() if c.isupper() else c for c in rpc).lstrip("_")
        impl = getattr(core, impl_name, None)
        if impl is None:
            continue

        def handler(self, request, context, _impl=impl):
            with core.lock:
                try:
                    return _impl(request)
                except Abort as e:
                    context.abort(e.code, e.message)
        methods[rpc] = handler
    return type(f"{service_name}Impl", (base,), methods)()


SERVICES = ["CatalogService", "MemberService", "LendingService", "HoldService", "BillingService",
            "TestSupportService"]


def start_server(port: int = 0, proto: str | Path = DEFAULT_PROTO, fixed: bool = False,
                 loan_days: int | None = None):
    """Starts the server in background threads. Returns (server, port, core)."""
    pb2, pb2_grpc = compile_proto(proto)
    if loan_days is None:  # v2 of the docs lengthened the loan period
        loan_days = 21 if hasattr(pb2, "UpdateMemberEmailRequest") else 14
    core = Shelfwise(pb2, fixed=fixed, loan_days=loan_days)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    for name in SERVICES:
        adder = getattr(pb2_grpc, f"add_{name}Servicer_to_server", None)
        if adder:
            adder(_servicer(pb2_grpc, name, core), server)
    bound = server.add_insecure_port(f"127.0.0.1:{port}")
    server.start()
    return server, bound, core


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=50051)
    ap.add_argument("--proto", default=str(DEFAULT_PROTO))
    ap.add_argument("--fixed", action="store_true", help="disable the planted bugs")
    a = ap.parse_args()
    server, port, _ = start_server(a.port, a.proto, a.fixed)
    print(f"Shelfwise listening on 127.0.0.1:{port} ({'fixed' if a.fixed else 'with planted bugs'})")
    server.wait_for_termination()


if __name__ == "__main__":
    main()
