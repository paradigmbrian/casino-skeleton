from casino.cards import Card
from casino.hand import Hand


def test_hand_value_simple():
    hand = Hand()
    hand.add(Card("10", "hearts"))
    hand.add(Card("7", "clubs"))
    assert hand.value() == 17


def test_hand_value_ace_soft():
    hand = Hand()
    hand.add(Card("A", "hearts"))
    hand.add(Card("6", "clubs"))
    assert hand.value() == 17


def test_hand_bust():
    hand = Hand()
    hand.add(Card("K", "hearts"))
    hand.add(Card("Q", "clubs"))
    hand.add(Card("5", "spades"))
    assert hand.is_bust()
