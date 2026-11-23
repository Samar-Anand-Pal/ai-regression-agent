"""Shelfwise Python SDK: thin, typed wrappers over the gRPC stubs.

Stage 3 of the pipeline reads this file to record which RPCs have an SDK
method. TestSupportService is deliberately left unwrapped (internal only).
"""
from __future__ import annotations

from pathlib import Path

import grpc

from .protoc import compile_proto

DEFAULT_PROTO = Path(__file__).parent / "protos" / "shelfwise.proto"


class ShelfwiseClient:
    def __init__(self, target: str = "127.0.0.1:50051", proto: str | Path = DEFAULT_PROTO):
        self.pb2, pb2_grpc = compile_proto(proto)
        self.channel = grpc.insecure_channel(target)
        self._catalog = pb2_grpc.CatalogServiceStub(self.channel)
        self._members = pb2_grpc.MemberServiceStub(self.channel)
        self._lending = pb2_grpc.LendingServiceStub(self.channel)
        self._holds = pb2_grpc.HoldServiceStub(self.channel)
        self._billing = pb2_grpc.BillingServiceStub(self.channel)

    # catalog
    def create_book(self, title, author, isbn, total_copies):
        return self._catalog.CreateBook(self.pb2.CreateBookRequest(
            title=title, author=author, isbn=isbn, total_copies=total_copies))

    def get_book(self, book_id):
        return self._catalog.GetBook(self.pb2.GetBookRequest(book_id=book_id))

    def list_books(self, author=""):
        return self._catalog.ListBooks(self.pb2.ListBooksRequest(author=author))

    def update_book_copies(self, book_id, total_copies):
        return self._catalog.UpdateBookCopies(self.pb2.UpdateBookCopiesRequest(
            book_id=book_id, total_copies=total_copies))

    def delete_book(self, book_id):
        return self._catalog.DeleteBook(self.pb2.DeleteBookRequest(book_id=book_id))

    # members
    def register_member(self, name, email):
        return self._members.RegisterMember(self.pb2.RegisterMemberRequest(name=name, email=email))

    def get_member(self, member_id):
        return self._members.GetMember(self.pb2.GetMemberRequest(member_id=member_id))

    def suspend_member(self, member_id, reason=""):
        return self._members.SuspendMember(self.pb2.SuspendMemberRequest(member_id=member_id, reason=reason))

    def reinstate_member(self, member_id):
        return self._members.ReinstateMember(self.pb2.ReinstateMemberRequest(member_id=member_id))

    # lending
    def checkout_book(self, book_id, member_id):
        return self._lending.CheckoutBook(self.pb2.CheckoutBookRequest(book_id=book_id, member_id=member_id))

    def return_book(self, loan_id):
        return self._lending.ReturnBook(self.pb2.ReturnBookRequest(loan_id=loan_id))

    def renew_loan(self, loan_id):
        return self._lending.RenewLoan(self.pb2.RenewLoanRequest(loan_id=loan_id))

    def get_loan(self, loan_id):
        return self._lending.GetLoan(self.pb2.GetLoanRequest(loan_id=loan_id))

    def list_member_loans(self, member_id):
        return self._lending.ListMemberLoans(self.pb2.ListMemberLoansRequest(member_id=member_id))

    # holds
    def place_hold(self, book_id, member_id):
        return self._holds.PlaceHold(self.pb2.PlaceHoldRequest(book_id=book_id, member_id=member_id))

    def cancel_hold(self, hold_id):
        return self._holds.CancelHold(self.pb2.CancelHoldRequest(hold_id=hold_id))

    def list_holds(self, book_id):
        return self._holds.ListHolds(self.pb2.ListHoldsRequest(book_id=book_id))

    # billing
    def get_account(self, member_id):
        return self._billing.GetAccount(self.pb2.GetAccountRequest(member_id=member_id))

    def pay_fine(self, member_id, amount_cents):
        return self._billing.PayFine(self.pb2.PayFineRequest(member_id=member_id, amount_cents=amount_cents))
