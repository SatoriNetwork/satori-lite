"""
Load tests for neuron client and server.

Tests concurrent request handling, multiple clients, and behavior under load.
"""
import pytest
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading


@pytest.mark.slow
@pytest.mark.integration
def test_concurrent_health_checks(server_url, server_available):
    """Test server handles multiple concurrent health check requests."""
    num_workers = 10
    requests_per_worker = 5

    def make_health_request():
        response = requests.get(f"{server_url}/health")
        return response.status_code == 200

    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(make_health_request)
            for _ in range(num_workers * requests_per_worker)
        ]

        results = [future.result() for future in as_completed(futures)]

    # All requests should succeed
    assert all(results), f"Only {sum(results)}/{len(results)} requests succeeded"


@pytest.mark.slow
@pytest.mark.integration
def test_concurrent_challenge_token_requests(server_url, server_available):
    """Test server handles concurrent challenge token requests."""
    num_concurrent = 15

    def get_challenge():
        response = requests.get(f"{server_url}/api/v1/auth/challenge")
        if response.status_code == 200:
            return response.json()["challenge"]
        return None

    with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
        futures = [executor.submit(get_challenge) for _ in range(num_concurrent)]
        tokens = [future.result() for future in as_completed(futures)]

    # All requests should succeed
    assert all(tokens), f"Only {len([t for t in tokens if t])}/{num_concurrent} succeeded"

    # All tokens should be unique
    unique_tokens = set([t for t in tokens if t])
    assert len(unique_tokens) == num_concurrent, "Tokens should be unique"


@pytest.mark.slow
@pytest.mark.integration
def test_concurrent_observation_requests(server_url, server_available):
    """Test server handles concurrent observation requests."""
    num_concurrent = 12

    def get_observation():
        try:
            response = requests.get(f"{server_url}/api/v1/observation/get")
            return response.status_code == 200
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
        futures = [executor.submit(get_observation) for _ in range(num_concurrent)]
        results = [future.result() for future in as_completed(futures)]

    success_rate = sum(results) / len(results) * 100

    # At least 95% should succeed
    assert success_rate >= 95, f"Only {success_rate:.1f}% success rate"


@pytest.mark.slow
@pytest.mark.integration
def test_mixed_concurrent_requests(server_url, server_available):
    """Test server handles mixed concurrent requests to different endpoints."""
    num_each = 8

    def make_request(endpoint):
        try:
            response = requests.get(f"{server_url}{endpoint}")
            return response.status_code == 200
        except Exception:
            return False

    endpoints = [
        "/health",
        "/",
        "/api/v1/auth/challenge",
        "/api/v1/observation/get",
    ]

    with ThreadPoolExecutor(max_workers=len(endpoints) * num_each) as executor:
        futures = []
        for endpoint in endpoints:
            for _ in range(num_each):
                futures.append(executor.submit(make_request, endpoint))

        results = [future.result() for future in as_completed(futures)]

    success_rate = sum(results) / len(results) * 100

    # At least 95% should succeed under load
    assert success_rate >= 95, f"Only {success_rate:.1f}% success rate with mixed requests"


@pytest.mark.slow
@pytest.mark.integration
def test_multiple_client_instances(test_alias, test_server_url):
    """Test multiple client instances can operate concurrently."""
    from satorilib.server.server import SatoriServerClient

    num_clients = 8

    def client_operation(client_id):
        client = SatoriServerClient(alias=test_alias, url=test_server_url)

        # Each client performs multiple operations
        results = []

        # Checkin
        checkin_result = client.checkin()
        results.append(isinstance(checkin_result, dict))

        # Publish (different topics to avoid rate limiting)
        publish_result = client.publish(
            topic=f"load-test-client-{client_id}",
            data="100.0",
            observationTime="2025-01-01T00:00:00Z",
            observationHash=f"hash-{client_id}",
            isPrediction=True
        )
        results.append(publish_result in [None, True])

        # Get challenge
        challenge = client._getChallenge()
        results.append(challenge is not None)

        return all(results)

    with ThreadPoolExecutor(max_workers=num_clients) as executor:
        futures = [
            executor.submit(client_operation, i)
            for i in range(num_clients)
        ]

        results = [future.result() for future in as_completed(futures)]

    # All clients should complete successfully
    success_rate = sum(results) / len(results) * 100
    assert success_rate >= 90, f"Only {success_rate:.1f}% of clients succeeded"


@pytest.mark.slow
@pytest.mark.integration
def test_burst_traffic_handling(server_url, server_available):
    """Test server handles burst traffic (many requests at once)."""
    burst_size = 30

    def make_burst_request():
        response = requests.get(f"{server_url}/health")
        return response.status_code

    # Send burst of requests all at once
    start = time.time()
    with ThreadPoolExecutor(max_workers=burst_size) as executor:
        futures = [executor.submit(make_burst_request) for _ in range(burst_size)]
        status_codes = [future.result() for future in as_completed(futures)]
    elapsed = time.time() - start

    # All should succeed
    success_count = sum(1 for code in status_codes if code == 200)
    success_rate = success_count / burst_size * 100

    assert success_rate >= 95, f"Only {success_rate:.1f}% succeeded in burst"

    # Should handle burst reasonably fast
    requests_per_second = burst_size / elapsed
    assert requests_per_second > 10, f"Only {requests_per_second:.1f} req/sec during burst"


@pytest.mark.slow
@pytest.mark.integration
def test_sustained_load(server_url, server_available):
    """Test server handles sustained load over time."""
    duration_seconds = 5
    num_workers = 5

    results = []
    stop_event = threading.Event()

    def sustained_worker():
        local_results = []
        while not stop_event.is_set():
            try:
                response = requests.get(f"{server_url}/health", timeout=2)
                local_results.append(response.status_code == 200)
            except Exception:
                local_results.append(False)
            time.sleep(0.1)  # Small delay between requests
        return local_results

    # Start workers
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [executor.submit(sustained_worker) for _ in range(num_workers)]

        # Let them run for duration
        time.sleep(duration_seconds)

        # Stop workers
        stop_event.set()

        # Collect results
        for future in as_completed(futures):
            results.extend(future.result())

    # Should maintain good success rate under sustained load
    success_rate = sum(results) / len(results) * 100
    assert success_rate >= 95, f"Only {success_rate:.1f}% success rate under sustained load"


@pytest.mark.slow
@pytest.mark.integration
def test_concurrent_authenticated_requests(server_url, server_available, test_alias):
    """Test server handles concurrent authenticated requests."""
    num_concurrent = 10

    def authenticated_request():
        try:
            # Get challenge
            challenge_response = requests.get(f"{server_url}/api/v1/auth/challenge")
            if challenge_response.status_code != 200:
                return False

            challenge = challenge_response.json()["challenge"]

            # Make authenticated call
            headers = test_alias.authPayload(asDict=True, challenge=challenge)
            response = requests.get(
                f"{server_url}/api/v1/balance/get",
                headers=headers
            )

            # Will fail auth (401) but tests server handles concurrent auth
            return response.status_code in [200, 401]
        except Exception:
            return False

    with ThreadPoolExecutor(max_workers=num_concurrent) as executor:
        futures = [executor.submit(authenticated_request) for _ in range(num_concurrent)]
        results = [future.result() for future in as_completed(futures)]

    success_rate = sum(results) / len(results) * 100

    # Should handle concurrent auth requests
    assert success_rate >= 90, f"Only {success_rate:.1f}% handled concurrent auth"


@pytest.mark.slow
@pytest.mark.integration
def test_connection_pooling_efficiency(server_url, server_available):
    """Test that connection pooling provides efficiency gains."""
    num_requests = 20

    # Without session (new connection each time)
    start_no_session = time.time()
    for _ in range(num_requests):
        requests.get(f"{server_url}/health")
    time_no_session = time.time() - start_no_session

    # With session (connection pooling)
    start_with_session = time.time()
    with requests.Session() as session:
        for _ in range(num_requests):
            session.get(f"{server_url}/health")
    time_with_session = time.time() - start_with_session

    # Session should be faster or similar (connection reuse)
    efficiency_gain = (time_no_session - time_with_session) / time_no_session * 100

    # Session should be at least as fast (allow for variance)
    assert time_with_session <= time_no_session * 1.2, \
        f"Session was slower: {time_with_session:.2f}s vs {time_no_session:.2f}s"
