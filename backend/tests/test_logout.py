def test_logout_returns_204_and_clears_cookie(client):
    client.cookies.set("saaschatbot_session", "fake-token")
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    set_cookie = response.headers.get("set-cookie", "")
    assert "saaschatbot_session=" in set_cookie.lower()
    assert "max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower()
