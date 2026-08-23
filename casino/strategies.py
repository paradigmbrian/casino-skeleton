class PlayerStrategy:
    """Base class for player strategies. Subclasses decide hit/stand."""

    name = "base"

    def should_hit(self, hand, dealer_upcard):
        raise NotImplementedError


class BasicPlayerStrategy(PlayerStrategy):
    """Hits until hand value reaches 17."""

    name = "basic_17"

    def should_hit(self, hand, dealer_upcard):
        return hand.value() < 17


class DealerStrategy:
    """Base class for dealer strategies."""

    name = "base"

    def should_hit(self, hand):
        raise NotImplementedError


class StandardDealerStrategy(DealerStrategy):
    """Standard casino rule: hit until 17, stand on 17+."""

    name = "standard_17"

    def should_hit(self, hand):
        return hand.value() < 17
