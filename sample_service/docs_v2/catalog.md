# Catalog

**Owner:** Catalog team · **Status:** Approved

The catalog is the list of books the library owns. Librarians maintain it; members browse it.

## Requirements

- **REQ-001** A librarian can add a book by giving its title, author, ISBN and the number of copies the library owns (between 1 and 50). A newly added book has all of its copies available.
- **REQ-002** Adding a book whose ISBN is already in the catalog is rejected with `ALREADY_EXISTS`.
- **REQ-003** Adding a book with fewer than 1 or more than 50 copies is rejected with `INVALID_ARGUMENT`.
- **REQ-004** Anyone can look up a book by its ID. Looking up an ID that does not exist returns `NOT_FOUND`.
- **REQ-006** A librarian can change the number of copies of a book. The number of available copies changes by the same amount. The total can never go below the number of copies currently on loan; such a change is rejected with `FAILED_PRECONDITION`.
- **REQ-007** A librarian can remove a book from the catalog, but only when none of its copies are on loan. Otherwise the removal is rejected with `FAILED_PRECONDITION`.

## Non-functional notes

- The catalog page should load in under 2 seconds on a typical home connection.
- Book covers are shown as thumbnails next to each title.
