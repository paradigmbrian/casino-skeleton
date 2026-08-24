import random
from collections import Counter

import pytest

from casino.cards import RANKS, SUITS, Card, Deck


def unshuffled_cards(num_decks=1):
    """The card multiset a Deck is expected to contain, in construction order."""
    return [(r, s) for _ in range(num_decks) for s in SUITS for r in RANKS]


@pytest.mark.parametrize(
    "rank,suit,expected",
    [
        ("A", "hearts", "AH"),
        ("10", "diamonds", "10D"),
        ("K", "clubs", "KC"),
        ("2", "spades", "2S"),
    ],
)
def test_card_repr_is_rank_plus_suit_initial(rank, suit, expected):
    assert repr(Card(rank, suit)) == expected


def test_card_keeps_rank_and_suit():
    card = Card("Q", "hearts")
    assert (card.rank, card.suit) == ("Q", "hearts")


def test_single_deck_has_52_cards():
    assert len(Deck().cards) == 52


@pytest.mark.parametrize("num_decks", [1, 2, 6])
def test_deck_size_scales_with_num_decks(num_decks):
    assert len(Deck(num_decks).cards) == 52 * num_decks


def test_deck_contains_every_rank_suit_combination_once():
    dealt = Counter((c.rank, c.suit) for c in Deck().cards)
    assert dealt == Counter(unshuffled_cards())


def test_multi_deck_contains_each_combination_once_per_deck():
    dealt = Counter((c.rank, c.suit) for c in Deck(3).cards)
    assert set(dealt) == set(unshuffled_cards())
    assert set(dealt.values()) == {3}


def test_deck_is_shuffled_on_construction():
    random.seed(1234)
    order = [(c.rank, c.suit) for c in Deck().cards]
    assert order != unshuffled_cards()


def test_same_seed_produces_same_deck_order():
    random.seed(7)
    first = [repr(c) for c in Deck().cards]
    random.seed(7)
    second = [repr(c) for c in Deck().cards]
    assert first == second


def test_different_seeds_produce_different_deck_orders():
    random.seed(1)
    first = [repr(c) for c in Deck().cards]
    random.seed(2)
    second = [repr(c) for c in Deck().cards]
    assert first != second


def test_draw_returns_a_card_and_removes_it_from_the_deck():
    random.seed(99)
    deck = Deck()
    remaining_before = list(deck.cards)

    card = deck.draw()

    assert isinstance(card, Card)
    assert card in remaining_before
    assert len(deck.cards) == 51
    assert card not in deck.cards


def test_drawing_the_whole_deck_yields_every_card_exactly_once():
    random.seed(2024)
    deck = Deck()
    drawn = [deck.draw() for _ in range(52)]

    assert deck.cards == []
    assert Counter((c.rank, c.suit) for c in drawn) == Counter(unshuffled_cards())


def test_draw_from_empty_deck_raises():
    deck = Deck()
    for _ in range(52):
        deck.draw()
    with pytest.raises(IndexError):
        deck.draw()
