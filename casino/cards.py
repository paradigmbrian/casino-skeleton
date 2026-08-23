import random

SUITS = ["hearts", "diamonds", "clubs", "spades"]
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]


class Card:
    def __init__(self, rank, suit):
        self.rank = rank
        self.suit = suit

    def __repr__(self):
        return f"{self.rank}{self.suit[0].upper()}"


class Deck:
    def __init__(self, num_decks=1):
        self.cards = [Card(r, s) for _ in range(num_decks) for s in SUITS for r in RANKS]
        random.shuffle(self.cards)

    def draw(self):
        return self.cards.pop()
