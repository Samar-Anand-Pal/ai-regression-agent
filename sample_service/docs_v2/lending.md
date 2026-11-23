# Lending

**Owner:** Circulation team · **Status:** Approved

Lending covers borrowing, returning and renewing books. The service keeps its own day counter ("service day"); every date below is measured in service days.

## Requirements

- **REQ-020** An active member can check out a book that has an available copy. The new loan is `ACTIVE`, is due 21 days after the checkout day, and the book's available copies go down by one.
- **REQ-021** Checking out a book with no available copies is rejected with `FAILED_PRECONDITION`.
- **REQ-022** A member can have at most 5 active loans at a time. A sixth checkout is rejected with `RESOURCE_EXHAUSTED`.
- **REQ-023** A member returns a book by its loan ID. The loan becomes `RETURNED` and the book's available copies go up by one.
- **REQ-024** A book returned after its due day is charged a fine of 25 cents for every day it is late. The fine is recorded on the loan and added to the member's account balance. A book returned on or before the due day has no fine.
- **REQ-025** A member can renew an active loan, which moves the due day 21 days later than the current due day. A loan can be renewed at most 2 times; a third renewal is rejected with `FAILED_PRECONDITION`.
- **REQ-026** A loan cannot be renewed while another member is waiting for the same book (has a hold on it); the renewal is rejected with `FAILED_PRECONDITION`.
- **REQ-027** A member can see the list of all their loans, active and returned.
- **REQ-028** Returning a loan that has already been returned is rejected with `FAILED_PRECONDITION`.
