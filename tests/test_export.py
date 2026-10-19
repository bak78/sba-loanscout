"""The results file and its seen-list."""


def test_a_new_results_file_keeps_the_memory_of_companies_already_done(tmp_path):
    """Deleting the results to start a fresh batch must not wipe the seen-list,
    or every company already done is looked up (and paid for) again."""
    from pathlib import Path

    from sba_loanscout.export import CsvWriter

    out = Path(tmp_path) / "leads.csv"
    with CsvWriter(out) as writer:
        writer.write({"key": "111"})
        writer.mark_done("222")
    out.unlink()  # a fresh batch: the results go, the seen-list stays

    with CsvWriter(out) as writer:
        assert writer.already_done("111") and writer.already_done("222")
        writer.write({"key": "333"})

    with CsvWriter(out) as writer:  # the next run (another state) still remembers everything
        assert all(writer.already_done(k) for k in ("111", "222", "333"))
