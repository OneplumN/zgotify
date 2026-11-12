package com.zgotify.app

import java.io.IOException
import java.util.concurrent.TimeUnit
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ApiTest {
    private lateinit var server: MockWebServer
    private lateinit var profile: Profile
    @Before fun start() {
        server = MockWebServer().apply { start() }
        profile = Profile(server.url("/").toString().removeSuffix("/"), "alice", "synthetic-token")
    }
    @After fun stop() { server.shutdown() }

    @Test fun malformedSuccessfulResponseCannotBeMistakenForAcknowledgement() {
        server.enqueue(MockResponse().setBody("<html>upstream error</html>"))
        assertThrows(IOException::class.java) { Api.request(profile, "GET", "/v1/me") }
    }

    @Test fun serverErrorBodyCannotLeakIntoUserFacingError() {
        server.enqueue(MockResponse().setResponseCode(500).setBody("{\"error\":\"secret-debug-value\"}"))
        val error = assertThrows(ApiException::class.java) { Api.request(profile, "GET", "/v1/me") }
        assertFalse(error.message.orEmpty().contains("secret-debug-value"))
    }

    @Test fun bearerCredentialStaysInHeaderAndRedirectsAreNotFollowed() {
        server.enqueue(MockResponse().setResponseCode(302).setHeader("Location", server.url("/elsewhere")))
        assertThrows(ApiException::class.java) { Api.request(profile, "GET", "/v1/me") }
        val request = server.takeRequest(1, TimeUnit.SECONDS)!!
        assertEquals("/v1/me", request.path)
        assertEquals("Bearer synthetic-token", request.getHeader("Authorization"))
        assertEquals(1, server.requestCount)
    }

    @Test fun empty204AcknowledgementIsAllowed() {
        server.enqueue(MockResponse().setResponseCode(204))
        assertEquals(0, Api.request(profile, "DELETE", "/v1/session").length())
    }

    @Test fun protectedEndpointsDoNotAcceptExternalCleartextServers() {
        assertThrows(IllegalArgumentException::class.java) { Api.normalizeServer("http://example.com") }
        assertThrows(IllegalArgumentException::class.java) { Api.normalizeServer("https://user:pass@example.com") }
        assertEquals("https://example.com", Api.normalizeServer("https://EXAMPLE.com/"))
    }

    @Test fun loginUsesAuthenticatedCanonicalIdentity() {
        server.enqueue(MockResponse().setBody("""{"token":"session-token","person":{"id":7,"username":"alice"}}"""))
        val authenticated = Api.login(profile.copy(username = "ALICE"), JSONObject())
        assertEquals(7L, authenticated.personId)
        assertEquals("alice", authenticated.username)
        assertEquals("${profile.server}|person:7", authenticated.cacheKey)
    }

    @Test fun invalidLoginIdentityIsRejectedAndIssuedSessionRevoked() {
        server.enqueue(MockResponse().setBody("""{"token":"session-token","person":{"id":"7","username":"alice"}}"""))
        server.enqueue(MockResponse().setBody("{}"))
        assertThrows(IOException::class.java) { Api.login(profile, JSONObject()) }
        assertEquals("/v1/login", server.takeRequest().path)
        val revoke = server.takeRequest(1, TimeUnit.SECONDS)!!
        assertEquals("DELETE", revoke.method)
        assertEquals("Bearer session-token", revoke.getHeader("Authorization"))
    }
}
