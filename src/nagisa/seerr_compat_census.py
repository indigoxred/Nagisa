"""Read a stable bounded list of existing Shoko file IDs for initial export."""

from __future__ import annotations

from typing import Any


class ShokoInitialCensus:
    def __init__(self, shoko: Any, *, page_size: int = 200,
                 max_files: int = 20000) -> None:
        if (not callable(getattr(shoko, 'request_json', None))
                or type(page_size) is not int or not 1 <= page_size <= 1000
                or type(max_files) is not int or not 1 <= max_files <= 32768):
            raise ValueError('bounded Shoko initial census required')
        self.shoko = shoko
        self.page_size = page_size
        self.max_files = max_files

    def _page(self, page: int, total: int | None) -> tuple[int, tuple[int, ...]]:
        path = (f'/api/v3/File?pageSize={self.page_size}&page={page}'
                '&sortOrder=FileID')
        response = self.shoko.request_json('GET', path)
        if (not isinstance(response, dict)
                or type(response.get('Total')) is not int
                or not 0 <= response['Total'] <= self.max_files
                or not isinstance(response.get('List'), list)
                or (total is not None and response['Total'] != total)):
            raise ValueError('Shoko initial census changed or exceeds bound')
        count = response['Total']
        expected = max(0, min(self.page_size,
                              count - (page - 1) * self.page_size))
        rows = response['List']
        if (len(rows) != expected
                or any(not isinstance(row, dict)
                       or type(row.get('ID')) is not int or row['ID'] <= 0
                       for row in rows)):
            raise ValueError('Shoko initial file page is incomplete')
        return count, tuple(row['ID'] for row in rows)

    def read_ids(self) -> tuple[int, ...]:
        total, first = self._page(1, None)
        result = list(first)
        pages = (total + self.page_size - 1) // self.page_size
        for page in range(2, pages + 1):
            _, ids = self._page(page, total)
            result.extend(ids)
        if (len(result) != total
                or any(left >= right for left, right in
                       zip(result, result[1:]))):
            raise ValueError('Shoko initial IDs skipped or repeated')
        _, first_again = self._page(1, total)
        _, last_again = self._page(max(1, pages), total)
        if (first_again != first
                or last_again != tuple(result[(max(1, pages) - 1)
                                              * self.page_size:])):
            raise ValueError('Shoko initial census changed during read')
        return tuple(result)
