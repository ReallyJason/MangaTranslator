import numpy as np
import pytest

from fukidashi.cache import cache_key
from fukidashi.detect import detect_boxes, merge_boxes, reading_order, tile_spans
from fukidashi.models import Block, TranslateOptions
from fukidashi.ocr import column_to_line, has_text, split_columns
from fukidashi.series import series_key


def test_merge_groups_nearby_boxes_only():
    # Three lines of one bubble (30px glyphs, 6px leading) + a far-away bubble.
    boxes = [(100, 100, 300, 130), (110, 136, 290, 166), (120, 172, 280, 202), (600, 100, 700, 130)]
    blocks = merge_boxes(boxes)
    assert len(blocks) == 2
    big = max(blocks, key=lambda b: len(b.boxes))
    assert big.rect == (100, 100, 300, 202)
    assert len(big.boxes) == 3


def test_merge_gap_threshold_is_relative_to_median_size():
    # Gap of 20px with 30px glyphs: 20 > 0.6*30=18 -> separate.
    assert len(merge_boxes([(0, 0, 30, 30), (50, 0, 80, 30)])) == 2
    # Gap of 15px -> merged.
    assert len(merge_boxes([(0, 0, 30, 30), (45, 0, 75, 30)])) == 1


def test_merge_is_transitive():
    boxes = [(0, 0, 30, 30), (40, 0, 70, 30), (80, 0, 110, 30), (120, 0, 150, 30)]
    assert len(merge_boxes(boxes)) == 1


def test_merge_empty():
    assert merge_boxes([]) == []


@pytest.mark.parametrize("h,w", [(1000, 800), (5000, 800), (20000, 720), (3001, 1500), (12345, 690)])
def test_tile_cores_cover_image_exactly_once(h, w):
    spans = tile_spans(h, w)
    assert spans[0][2] == 0 and spans[-1][3] == h
    for (s, e, cs, ce), nxt in zip(spans, spans[1:] + [None]):
        assert s <= cs < ce <= e
        assert e - s <= 2560
        if nxt:
            assert ce == nxt[2]  # cores are contiguous
            assert nxt[0] < e  # tiles overlap
            assert e - ce >= 100 and nxt[2] - nxt[0] >= 100  # core edge is well inside both tiles


def test_short_image_is_one_tile():
    assert tile_spans(1200, 900) == [(0, 1200, 0, 1200)]


class FakeReader:
    """Reports a text line wherever the tile contains one of the given rows."""

    def __init__(self, lines):
        self.lines = lines  # absolute (x0, y0, x1, y1)
        self.offset = 0
        self.calls = []

    def detect(self, tile, **kw):
        h = tile.shape[0]
        start = self.calls_start.pop(0)
        self.calls.append((start, h))
        out = []
        for x0, y0, x1, y1 in self.lines:
            ty0, ty1 = max(y0 - start, 0), min(y1 - start, h)
            if ty1 > ty0:
                out.append([x0, x1, ty0, ty1])  # clipped at tile edges, like a real detector
        return [out], [[]]


def test_detect_boxes_tiles_tall_strip_and_dedupes_overlap():
    h, w = 9000, 800
    lines = [(100, 50, 400, 80), (100, 2000, 400, 2030), (100, 4440, 400, 4470), (100, 8900, 400, 8930)]
    reader = FakeReader(lines)
    reader.calls_start = [s for s, *_ in tile_spans(h, w)]
    boxes = detect_boxes(reader, np.zeros((h, w, 3), np.uint8))
    assert len(reader.calls) > 3  # it was tiled
    assert sorted(boxes) == sorted(lines)  # each line exactly once, in page coordinates


def test_reading_order_right_to_left_rows():
    a = Block(500, 100, 560, 300)  # top right
    b = Block(100, 120, 160, 280)  # top left
    c = Block(300, 600, 360, 700)  # bottom
    assert reading_order([b, c, a], right_to_left=True) == [a, b, c]
    assert reading_order([a, c, b], right_to_left=False) == [b, a, c]


def test_has_text():
    assert has_text("おい") and has_text("A") and has_text("12")
    assert not has_text("…!?") and not has_text("  ") and not has_text("")


def test_split_columns_and_relayout():
    img = np.full((200, 120, 3), 255, np.uint8)
    img[10:190, 80:110] = 0  # right column
    img[10:120, 20:50] = 0  # left column
    img[10:60, 62:66] = 0  # furigana sliver: dropped
    cols = split_columns(img)
    assert cols == [(80, 110), (20, 50)]
    col = np.full((5 * 36 + 10, 40, 3), 255, np.uint8)
    for i in range(5):  # five 30px "glyphs" with 6px gaps
        col[5 + i * 36 : 35 + i * 36, 5:35] = 0
    line = column_to_line(col)
    assert line.shape[1] > 3 * line.shape[0]  # vertical column became a horizontal line
    # Ink is laid out as 5 separate cells left to right.
    ink_cols = (line.mean(axis=2) < 128).any(axis=0)
    runs = np.count_nonzero(np.diff(ink_cols.astype(int)) == 1) + int(ink_cols[0])
    assert runs == 5


def test_series_key():
    assert series_key("https://www.site.com/manga/one-piece/chapter-1050?page=2") == "site.com/manga/one-piece"
    assert series_key("https://site.com/manga/one-piece/chapter-1051/") == "site.com/manga/one-piece"
    assert series_key("https://reader.example.org/read") == "reader.example.org"
    assert series_key("") == ""


def test_cache_key_ignores_session_only():
    data = b"image-bytes"
    a = TranslateOptions(session="https://x.com/s/1").cache_dict()
    b = TranslateOptions(session="https://y.com/other/2").cache_dict()
    c = TranslateOptions(target_lang="fr").cache_dict()
    assert cache_key(data, a) == cache_key(data, b)
    assert cache_key(data, a) != cache_key(data, c)
    assert cache_key(data, a) != cache_key(b"other", a)


def test_options_validation():
    with pytest.raises(ValueError):
        TranslateOptions.from_dict({"mode": "slow"})
    with pytest.raises(ValueError):
        TranslateOptions.from_dict({"engine": "babelfish"})
    o = TranslateOptions.from_dict({})
    assert (o.mode, o.engine, o.source_lang, o.target_lang) == ("context", "auto", "ja", "en")
