"""Generate a local synthetic cohort compatible with the dashboard. See docs/synthetic-data.md."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data import synthetic_cohort, synthetic_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--participants", type=int, default=24)
    parser.add_argument("--min-days", type=int, default=90)
    parser.add_argument("--max-days", type=int, default=180)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--config", type=Path, help="JSON file overriding any of data.SYNTHETIC_DEFAULTS")
    parser.add_argument("--print-config", action="store_true", help="print the full settings as JSON and exit")
    parser.add_argument("--output", type=Path, default=ROOT / "synthetic_data" / "synthetic_he.csv.gz")
    args = parser.parse_args()

    config = synthetic_config(json.loads(args.config.read_text()) if args.config else None)
    if args.print_config:
        print(json.dumps(config, indent=2))
        return
    df = synthetic_cohort(args.participants, args.seed, args.min_days, args.max_days, config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.output, index=False, compression="infer")
    # Everything needed to regenerate this exact file.
    recipe = {"participants": args.participants, "min_days": args.min_days, "max_days": args.max_days,
              "seed": args.seed, "config": config}
    recipe_path = args.output.with_name(args.output.name.split(".")[0] + ".settings.json")
    recipe_path.write_text(json.dumps(recipe, indent=2))
    print(f"Generated {df['name'].nunique()} participants and {len(df)} participant-days")
    print(f"Saved to {args.output.resolve()} (settings: {recipe_path.name})")


if __name__ == "__main__":
    main()
