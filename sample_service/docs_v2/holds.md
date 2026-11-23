# Holds

**Owner:** Circulation team · **Status:** Approved

A hold is a place in the queue for a book whose copies are all on loan.

## User stories

- As a member, I want to join the queue for a popular book so that I get it as soon as a copy comes back.

## Requirements

- **REQ-030** A member can place a hold on a book. The hold is `WAITING` and its position is its place in the queue for that book (1 = next in line).
- **REQ-031** A member cannot place a second hold on a book they are already waiting for; the request is rejected with `ALREADY_EXISTS`.
- **REQ-032** When a copy is returned and members are waiting, the hold in position 1 becomes `READY` and the returned copy is set aside for that member, so it does not count as available to anyone else.
- **REQ-033** A copy set aside for a `READY` hold can only be checked out by the member who placed that hold. Anyone else trying to check out that book is rejected with `FAILED_PRECONDITION`.
- **REQ-034** When the member with the `READY` hold checks out the book, their hold becomes `FULFILLED`.
- **REQ-035** A member can cancel their hold. The hold becomes `CANCELLED` and everyone behind it in the queue moves up one position.
- **REQ-036** Anyone can list the holds on a book, in queue order.
