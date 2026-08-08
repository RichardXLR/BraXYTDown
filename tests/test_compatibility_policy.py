from baixatube.compatibility import CompatibilityIssue, classify_youtube_failure


def test_youtube_failures_are_classified_before_autocure():
    assert classify_youtube_failure("HTTP Error 429: Too Many Requests") == CompatibilityIssue.RATE_LIMIT
    assert classify_youtube_failure("This video is not available in your country") == CompatibilityIssue.REGION
    assert classify_youtube_failure("nsig extraction failed: challenge solving failed") == CompatibilityIssue.EJS
    assert classify_youtube_failure("PO Token required for GVS") == CompatibilityIssue.PO_TOKEN
    assert classify_youtube_failure("connection reset by peer") == CompatibilityIssue.NETWORK
