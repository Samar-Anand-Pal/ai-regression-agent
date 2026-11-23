# Fines and payments

**Owner:** Finance · **Status:** Approved

## Requirements

- **REQ-040** A member can see their account balance, which is the total of their unpaid fines in cents.
- **REQ-041** A member can pay part or all of their balance. Paying reduces the balance by the amount paid. Paying more than the current balance, or a non-positive amount, is rejected with `INVALID_ARGUMENT`.
- **REQ-042** A member whose balance is more than 500 cents cannot check out books; the checkout is rejected with `FAILED_PRECONDITION` until they pay enough to bring the balance to 500 cents or less.

## Out of scope for the API

- A payment receipt is emailed to the member after each payment.
