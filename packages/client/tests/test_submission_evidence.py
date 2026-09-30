import pytest
from pursers_client.submission_evidence import submission_identity, rejection_fingerprint


@pytest.mark.parametrize('separator',['@',' @ ','\t@\t'])
def test_legacy_branch_format_is_semantically_equivalent(separator):
    assert submission_identity({'notes':f'branch_and_commit: pursers/TK-one{separator}'+ 'A'*40}) == ('pursers/TK-one','a'*40)


def test_structured_evidence_and_legacy_conflicts_fail_closed():
    evidence={'branch':'pursers/TK-one','commit_hash':'a'*40}
    assert submission_identity(evidence)==('pursers/TK-one','a'*40)
    assert submission_identity({**evidence,'notes':'branch_and_commit: pursers/TK-other@'+'b'*40})==('','')
    assert submission_identity({'notes':'branch_and_commit: pursers/TK-one@'+'a'*40+'\nbranch_and_commit: pursers/TK-other@'+'b'*40})==('','')


def test_rejection_fingerprint_normalizes_layout_but_not_different_work():
    assert rejection_fingerprint('Fix  the\nTEST')==rejection_fingerprint('fix the test')
    assert rejection_fingerprint('fix test')!=rejection_fingerprint('fix authentication')
    assert rejection_fingerprint(None)==''
