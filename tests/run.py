#!/usr/bin/env python3
"""Run the microtask unittest suite in parallel across test classes."""
from __future__ import annotations

import argparse
import compileall
from concurrent.futures import ProcessPoolExecutor, as_completed
import os
from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent


def _iter_test_classes(suite: unittest.TestSuite):
    seen: set[tuple[str, str]] = set()
    stack = [suite]
    while stack:
        current = stack.pop()
        if isinstance(current, unittest.TestSuite):
            stack.extend(reversed(list(current)))
        else:
            cls = current.__class__
            key = (cls.__module__, cls.__qualname__)
            if key not in seen:
                seen.add(key)
                yield key


class _TimingResult(unittest.TestResult):
    def __init__(self) -> None:
        super().__init__()
        self.timings: list[tuple[float, str]] = []
        self._started: dict[str, float] = {}

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._started[test.id()] = time.perf_counter()

    def stopTest(self, test: unittest.TestCase) -> None:
        started = self._started.pop(test.id(), None)
        if started is not None:
            self.timings.append((time.perf_counter() - started, test.id()))
        super().stopTest(test)


def _run_class(module_name: str, class_qualname: str) -> dict:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(f"{module_name}.{class_qualname}")
    result = _TimingResult()
    t0 = time.perf_counter()
    suite.run(result)
    elapsed = time.perf_counter() - t0
    return {
        "name": f"{module_name}.{class_qualname}",
        "elapsed": elapsed,
        "tests_run": result.testsRun,
        "failures": [(test.id(), tb) for test, tb in result.failures],
        "errors": [(test.id(), tb) for test, tb in result.errors],
        "skipped": [(test.id(), reason) for test, reason in result.skipped],
        "timings": result.timings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run microtask tests in parallel by test class.")
    parser.add_argument("targets", nargs="*", help="optional test file paths or module names")
    parser.add_argument("-j", "--jobs", type=int, default=min(os.cpu_count() or 4, 8),
                        help="worker processes (default: min(cpu_count, 8))")
    parser.add_argument("--slowest", type=int, default=15, help="number of slowest tests to show")
    parser.add_argument("--budget", type=float, default=None,
                        help="maximum allowed wall-clock seconds (defaults to 60s for full suite)")
    args = parser.parse_args()

    compileall.compile_dir(ROOT / "scripts", quiet=1)
    compileall.compile_dir(ROOT / "tools", quiet=1)

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(TESTS_DIR) not in sys.path:
        sys.path.insert(0, str(TESTS_DIR))

    loader = unittest.TestLoader()
    if args.targets:
        suite = unittest.TestSuite()
        for target in args.targets:
            path = Path(target)
            if path.exists():
                resolved = path.resolve()
                name = resolved.stem
            else:
                name = target
            suite.addTests(loader.loadTestsFromName(name))
    else:
        suite = loader.discover(start_dir=str(TESTS_DIR), top_level_dir=str(TESTS_DIR))

    classes = list(_iter_test_classes(suite))
    t0 = time.perf_counter()
    total_tests = 0
    all_failures: list[tuple[str, str]] = []
    all_errors: list[tuple[str, str]] = []
    all_skipped: list[tuple[str, str]] = []
    all_timings: list[tuple[float, str]] = []

    with ProcessPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(_run_class, mod, cls): (mod, cls) for mod, cls in classes}
        for future in as_completed(futures):
            report = future.result()
            total_tests += report["tests_run"]
            all_failures.extend(report["failures"])
            all_errors.extend(report["errors"])
            all_skipped.extend(report["skipped"])
            all_timings.extend(report["timings"])
            status = "FAIL" if (report["failures"] or report["errors"]) else "ok"
            print(f"[{status:4s}] {report['name']} ({report['tests_run']} tests, {report['elapsed']:.2f}s)")

    wall = time.perf_counter() - t0
    print()
    for label, items in (("FAIL", all_failures), ("ERROR", all_errors)):
        for test_id, tb in items:
            print("=" * 70)
            print(f"{label}: {test_id}")
            print("-" * 70)
            print(tb.rstrip())

    if args.slowest > 0 and all_timings:
        print(f"\nSlowest {min(args.slowest, len(all_timings))} tests:")
        for dt, test_id in sorted(all_timings, reverse=True)[: args.slowest]:
            print(f"  {dt:6.2f}s  {test_id}")

    print("-" * 70)
    print(f"Ran {total_tests} tests across {len(classes)} classes in {wall:.2f}s "
          f"(failures={len(all_failures)}, errors={len(all_errors)}, skipped={len(all_skipped)})")
    budget = (60.0 if not args.targets else 0.0) if args.budget is None else args.budget
    if budget > 0 and wall > budget:
        print(f"TIME BUDGET EXCEEDED: {wall:.2f}s > {budget:.2f}s", file=sys.stderr)
        return 1
    return 1 if (all_failures or all_errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
