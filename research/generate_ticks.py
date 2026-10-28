"""Generate reproducible SYNTHETIC top-of-book data; never label it exchange data."""

import argparse
import csv
from pathlib import Path
import random


def generate(output: Path, rows: int, seed: int) -> None:
    if not 2 <= rows <= 1_000_000:
        raise ValueError('rows must be between 2 and 1,000,000')
    rng = random.Random(seed)
    output.parent.mkdir(parents=True, exist_ok=True)
    bid = 10_000
    with output.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['time_ns', 'sequence', 'instrument', 'bid_paise', 'ask_paise',
                         'bid_quantity', 'ask_quantity', 'session_open'])
        for index in range(rows):
            bid = min(10_500, max(9_500, bid + rng.choice([-15, -10, -5, 0, 5, 10, 15])))
            writer.writerow([index * 100_000_000, index + 1, 1, bid, bid + 10,
                             rng.randrange(21), rng.randrange(21), int(index + 1 < rows)])


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('reports/synthetic_ticks.csv'))
    parser.add_argument('--rows', type=int, default=1_000)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    generate(args.output, args.rows, args.seed)
