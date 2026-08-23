from .monitor import Monitor
from .strategies import BasicPlayerStrategy, StandardDealerStrategy
from .table import Table


def run(num_rounds=100):
    table = Table(BasicPlayerStrategy(), StandardDealerStrategy())
    monitor = Monitor()
    for _ in range(num_rounds):
        outcome = table.play_round()
        monitor.record(outcome)
    print(f"Simulated {num_rounds} rounds. See outcomes.jsonl")


if __name__ == "__main__":
    run()
