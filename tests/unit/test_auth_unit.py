"""
Unit tests for authentication methods in SatoriServerClient.

Tests authentication-related methods in isolation with mocked dependencies.
"""
import pytest
from unittest.mock import Mock, patch, MagicMock
import json
import time
import requests


@pytest.mark.unit
def test_get_challenge_success(client_instance, mock_response):
    """Test _getChallenge() successfully gets challenge from server."""
    # Mock successful response
    mock_response.status_code = 200
    mock_response.json.return_value = {"challenge": "test-uuid-12345"}

    with patch('requests.get', return_value=mock_response):
        challenge = client_instance._getChallenge()

    assert challenge == "test-uuid-12345"


@pytest.mark.unit
def test_get_challenge_fallback_to_timestamp(client_instance):
    """Test _getChallenge() falls back to timestamp when server unavailable."""
    # Mock failed request
    with patch('requests.get', side_effect=requests.exceptions.RequestException("Connection error")):
        with patch('time.time', return_value=1234567890.5):
            challenge = client_instance._getChallenge()

    # Should return timestamp as string
    assert challenge == "1234567890.5"


@pytest.mark.unit
def test_get_challenge_fallback_on_non_200(client_instance, mock_response):
    """Test _getChallenge() falls back to timestamp on non-200 status."""
    mock_response.status_code = 500

    with patch('requests.get', return_value=mock_response):
        with patch('time.time', return_value=9999999.0):
            challenge = client_instance._getChallenge()

    assert challenge == "9999999.0"


@pytest.mark.unit
def test_get_challenge_fallback_on_missing_challenge_field(client_instance, mock_response):
    """Test _getChallenge() falls back when response missing 'challenge' field."""
    mock_response.status_code = 200
    mock_response.json.return_value = {"error": "no challenge"}

    with patch('requests.get', return_value=mock_response):
        with patch('time.time', return_value=7777777.0):
            challenge = client_instance._getChallenge()

    # Should return timestamp since challenge field missing
    assert challenge == "7777777.0"


@pytest.mark.unit
def test_make_authenticated_call_headers_generation(client_instance, mock_response, test_alias):
    """Test _makeAuthenticatedCall() generates correct authentication headers."""
    challenge = "test-challenge-123"
    # NOTE: the real key class exposes 'pubkey', NOT 'publicKey'
    expected_auth_payload = {
        "alias-pubkey": test_alias.pubkey,
        "message": challenge,
        "signature": "test_signature"
    }

    # Mock the alias's authPayload method
    test_alias.authPayload.return_value = expected_auth_payload

    with patch('requests.post', return_value=mock_response) as mock_post:
        client_instance._makeAuthenticatedCall(
            function=requests.post,
            endpoint="/test/endpoint",
            challenge=challenge
        )

    # Verify the call was made with correct headers
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args[1]

    assert "headers" in call_kwargs
    headers = call_kwargs["headers"]
    assert "alias-pubkey" in headers
    assert "message" in headers
    assert "signature" in headers


@pytest.mark.unit
def test_make_authenticated_call_with_payload(client_instance, mock_response):
    """Test _makeAuthenticatedCall() sends JSON payload correctly."""
    payload = {"value": "123.45", "observed_at": "2025-01-01T00:00:00Z"}

    with patch('requests.post', return_value=mock_response) as mock_post:
        client_instance._makeAuthenticatedCall(
            function=requests.post,
            endpoint="/api/v1/prediction/post",
            payload=payload
        )

    # Verify payload was passed (as JSON string since dict is serialized)
    call_kwargs = mock_post.call_args[1]
    assert "json" in call_kwargs
    # Payload is serialized to JSON string by _makeAuthenticatedCall
    assert call_kwargs["json"] == json.dumps(payload)


@pytest.mark.unit
def test_make_authenticated_call_raises_on_error(client_instance, mock_response):
    """Test _makeAuthenticatedCall() raises exception on HTTP error."""
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"

    def raise_error():
        raise requests.exceptions.HTTPError("401 Unauthorized")

    mock_response.raise_for_status = raise_error

    with patch('requests.post', return_value=mock_response):
        with pytest.raises(requests.exceptions.HTTPError):
            client_instance._makeAuthenticatedCall(
                function=requests.post,
                endpoint="/api/v1/prediction/post"
            )


@pytest.mark.unit
def test_make_authenticated_call_no_raise_when_disabled(client_instance, mock_response):
    """Test _makeAuthenticatedCall() doesn't raise when raiseForStatus=False."""
    mock_response.status_code = 500
    mock_response.text = "Server Error"

    with patch('requests.post', return_value=mock_response):
        # Should not raise
        response = client_instance._makeAuthenticatedCall(
            function=requests.post,
            endpoint="/api/v1/prediction/post",
            raiseForStatus=False
        )

    assert response.status_code == 500


@pytest.mark.unit
def test_make_authenticated_call_uses_custom_challenge(client_instance, mock_response, test_alias):
    """Test _makeAuthenticatedCall() uses provided challenge instead of fetching new one."""
    custom_challenge = "custom-challenge-xyz"

    with patch('requests.post', return_value=mock_response):
        with patch.object(client_instance, '_getChallenge') as mock_get_challenge:
            client_instance._makeAuthenticatedCall(
                function=requests.post,
                endpoint="/test",
                challenge=custom_challenge
            )

            # Should NOT call _getChallenge since challenge was provided
            mock_get_challenge.assert_not_called()

    # Verify the alias was called with the custom challenge
    test_alias.authPayload.assert_called()
    call_kwargs = test_alias.authPayload.call_args[1]
    assert call_kwargs.get('challenge') == custom_challenge


@pytest.mark.unit
def test_make_authenticated_call_gets_challenge_when_not_provided(client_instance, mock_response):
    """Test _makeAuthenticatedCall() fetches challenge when not provided."""
    with patch('requests.post', return_value=mock_response):
        with patch.object(client_instance, '_getChallenge', return_value="auto-challenge") as mock_get_challenge:
            client_instance._makeAuthenticatedCall(
                function=requests.post,
                endpoint="/test",
                challenge=None  # No challenge provided
            )

            # Should call _getChallenge
            mock_get_challenge.assert_called_once()


@pytest.mark.unit
def test_make_authenticated_call_extra_headers(client_instance, mock_response):
    """Test _makeAuthenticatedCall() merges extra headers correctly."""
    extra_headers = {"X-Custom-Header": "custom-value"}

    with patch('requests.post', return_value=mock_response) as mock_post:
        client_instance._makeAuthenticatedCall(
            function=requests.post,
            endpoint="/test",
            extraHeaders=extra_headers
        )

    call_kwargs = mock_post.call_args[1]
    headers = call_kwargs["headers"]

    # Should have both auth headers and custom header
    assert "X-Custom-Header" in headers
    assert headers["X-Custom-Header"] == "custom-value"
    assert "alias-pubkey" in headers
