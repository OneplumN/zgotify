package com.zgotify.app

import java.io.IOException
import java.util.concurrent.TimeUnit
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.RecordedRequest
import org.json.JSONArray
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
class ApiInboxRemoteTest {
    private lateinit var server: MockWebServer
    private lateinit var profile: Profile
    private lateinit var remote: ApiInboxRemote

    @Before fun start() {
        server = MockWebServer().apply { start() }
        profile = Profile(server.url("/").toString().removeSuffix("/"), "alice", "synthetic-token", 11)
        remote = ApiInboxRemote(profile)
    }

    @After fun stop() { server.shutdown() }

    @Test fun meReadsPersonFromBackendSessionWrapper() {
        enqueue("""{"person":{"id":11,"username":"alice","display_name":"Alice","admin":false},"device_name":"Phone A"}""")
        assertEquals(11L, remote.personId())
        request("GET", "/v1/me")
    }

    @Test fun meReturnsServerPersonRatherThanCachedIdentityForMismatchDetection() {
        enqueue("""{"person":{"id":22,"username":"bob","display_name":"Bob","admin":false},"device_name":"Phone A"}""")
        assertEquals(22L, remote.personId())
        request("GET", "/v1/me")
    }

    @Test fun meRejectsMissingOrInvalidPersonIdentity() {
        for (payload in listOf("{}", """{"id":11}""", """{"person":null}""",
            """{"person":{"id":0}}""", """{"person":{"id":-1}}""",
            """{"person":{"id":"11"}}""", """{"person":{"id":11.5}}""")) {
            enqueue(payload)
            assertThrows(IOException::class.java) { remote.personId() }
            request("GET", "/v1/me")
        }
    }

    @Test fun topicsReadsMembershipWrapperAndAllowsEmptySnapshot() {
        enqueue("""{"topics":[{"id":3,"name":"Operations"},{"id":9,"name":"Builds"}]}""")
        assertEquals(setOf(3L, 9L), remote.topics())
        request("GET", "/v1/topics")
        enqueue("""{"topics":[]}""")
        assertEquals(emptySet<Long>(), remote.topics())
        request("GET", "/v1/topics")
    }

    @Test fun topicsRejectsMalformedSnapshotInsteadOfReportingNoMemberships() {
        for (payload in listOf("{}", """{"topics":null}""", """{"topics":{}}""",
            """{"topics":[{"id":0,"name":"Bad"}]}""",
            """{"topics":[{"id":"3","name":"Bad"}]}""",
            """{"topics":[{"id":3,"name":"First"},{"id":3,"name":"Duplicate"}]}""")) {
            enqueue(payload)
            assertThrows(IOException::class.java) { remote.topics() }
            request("GET", "/v1/topics")
        }
    }

    @Test fun messagesReadsInboxWrapperWithHiddenStateAndOlderCursor() {
        val second = messageJson(41).put("state", JSONObject("""{"read":false,"hidden":false,"favorite":false,"version":0}"""))
        enqueue(JSONObject().put("messages", JSONArray().put(messageJson()).put(second)).toString())
        assertEquals(listOf(
            sample(),
            InboxMessage(41, 3, "CPU alert", "High load", 8, "2026-11-12T01:02:03.456Z", PersonalState()),
        ), remote.messages(43))
        request("GET", "/v1/messages?include_hidden=true&before=43")
    }

    @Test fun messagesAllowsEmptyFirstPage() {
        enqueue("""{"messages":[]}""")
        assertEquals(emptyList<InboxMessage>(), remote.messages(0))
        request("GET", "/v1/messages?include_hidden=true&before=0")
    }

    @Test fun messagesRejectsMalformedUnorderedDuplicateOrOutOfCursorPages() {
        val payloads = listOf(
            "{}", """{"messages":null}""", """{"messages":{}}""",
            JSONObject().put("messages", JSONArray().put(messageJson(41)).put(messageJson(42))).toString(),
            JSONObject().put("messages", JSONArray().put(messageJson()).put(messageJson())).toString(),
            JSONObject().put("messages", JSONArray().put(messageJson(43))).toString(),
        )
        for (payload in payloads) {
            enqueue(payload)
            assertThrows(IOException::class.java) { remote.messages(43) }
            request("GET", "/v1/messages?include_hidden=true&before=43")
        }
    }

    @Test fun messagesRejectsMoreThan100OtherwiseValidMessages() {
        val tooMany = JSONArray().apply { for (id in 101L downTo 1L) put(messageJson(id)) }
        enqueue(JSONObject().put("messages", tooMany).toString())
        assertThrows(IOException::class.java) { remote.messages(102) }
        request("GET", "/v1/messages?include_hidden=true&before=102")
    }

    @Test fun negativeInboxCursorIsRejectedBeforeNetwork() {
        enqueue("""{"messages":[]}""")
        assertThrows(IOException::class.java) { remote.messages(-1) }
        assertEquals(0, server.requestCount)
    }

    @Test fun detailReadsMessageWrapperIncludingOwnHiddenState() {
        enqueue(JSONObject().put("message", messageJson()).toString())
        assertEquals(sample(), remote.message(42))
        request("GET", "/v1/messages/42")
    }

    @Test fun detailRejectsWrongMessageIdentityOrWrapper() {
        for (payload in listOf("{}", messageJson().toString(), """{"message":null}""",
            JSONObject().put("message", messageJson(99)).toString())) {
            enqueue(payload)
            assertThrows(IOException::class.java) { remote.message(42) }
            request("GET", "/v1/messages/42")
        }
    }

    @Test fun inboxAndDetailRejectInvalidMessageFieldsWithoutCoercion() {
        val invalid = listOf(
            messageJson().put("id", 0),
            messageJson().put("id", "42"),
            messageJson().put("topic_id", -1),
            messageJson().put("title", 12),
            messageJson().put("message", JSONObject.NULL),
            messageJson().put("priority", -1),
            messageJson().put("priority", 11),
            messageJson().put("priority", 1.5),
            messageJson().put("created_at", JSONObject.NULL),
            messageJson().put("state", JSONObject.NULL),
            messageJson().put("state", stateJson().put("read", "true")),
            messageJson().put("state", stateJson().put("hidden", 1)),
            messageJson().put("state", stateJson().put("favorite", JSONObject.NULL)),
            messageJson().put("state", stateJson().put("version", -1)),
            messageJson().put("state", stateJson().put("version", "7")),
        )
        for (json in invalid) {
            enqueue(JSONObject().put("messages", JSONArray().put(json)).toString())
            assertThrows(IOException::class.java) { remote.messages(0) }
            request("GET", "/v1/messages?include_hidden=true&before=0")
            enqueue(JSONObject().put("message", json).toString())
            assertThrows(IOException::class.java) { remote.message(42) }
            request("GET", "/v1/messages/42")
        }
    }

    @Test fun patchReadsDirectStateAndSendsOnlyTouchedFields() {
        enqueue("""{"read":false,"hidden":true,"favorite":true,"version":8}""")
        val result = remote.patch(PendingState(profile.cacheKey, 42, 7, read = false, favorite = true,
            revision = 9, attempts = 2, nextAttemptAt = 1000))
        assertEquals(PersonalState(read = false, hidden = true, favorite = true, version = 8), result)
        val sent = request("PATCH", "/v1/messages/42/state")
        assertEquals("application/json; charset=utf-8", sent.getHeader("Content-Type"))
        val body = JSONObject(sent.body.readUtf8())
        assertEquals(setOf("expected_version", "read", "favorite"), body.keys().asSequence().toSet())
        assertEquals(7L, body.getLong("expected_version"))
        assertEquals(false, body.get("read"))
        assertEquals(true, body.get("favorite"))
    }

    @Test fun patchSendsExplicitHiddenFalseWithoutUntouchedFlags() {
        enqueue("""{"read":true,"hidden":false,"favorite":false,"version":8}""")
        assertEquals(PersonalState(read = true, hidden = false, favorite = false, version = 8),
            remote.patch(PendingState(profile.cacheKey, 42, 7, hidden = false)))
        val body = JSONObject(request("PATCH", "/v1/messages/42/state").body.readUtf8())
        assertEquals(setOf("expected_version", "hidden"), body.keys().asSequence().toSet())
        assertEquals(7L, body.getLong("expected_version"))
        assertEquals(false, body.get("hidden"))
    }

    @Test fun patchRejectsInvalidDirectStatesAndInventedStateWrapper() {
        val invalid = listOf(
            "{}", """{"ok":true}""",
            stateJson().apply { remove("read") }.toString(),
            stateJson().put("read", "true").toString(),
            stateJson().put("hidden", 0).toString(),
            stateJson().put("favorite", JSONObject.NULL).toString(),
            stateJson().put("version", -1).toString(),
            stateJson().put("version", "8").toString(),
            stateJson().put("version", 8.5).toString(),
            JSONObject().put("state", stateJson()).toString(),
        )
        for (payload in invalid) {
            enqueue(payload)
            assertThrows(IOException::class.java) {
                remote.patch(PendingState(profile.cacheKey, 42, 7, read = true))
            }
            request("PATCH", "/v1/messages/42/state")
        }
    }

    @Test fun patchRejectsOtherPersonOtherServerAndUsernameBasedAccountsBeforeNetwork() {
        for (account in listOf(profile.copy(personId = 22).cacheKey,
            profile.copy(server = "https://other.example").cacheKey, profile.key)) {
            enqueue("""{"read":true,"hidden":true,"favorite":false,"version":8}""")
            assertThrows(IOException::class.java) {
                remote.patch(PendingState(account, 42, 7, read = true))
            }
        }
        assertEquals(0, server.requestCount)
    }

    @Test fun patchRejectsNonPositiveMessageIdBeforeNetwork() {
        enqueue("""{"read":true,"hidden":true,"favorite":false,"version":8}""")
        assertThrows(IOException::class.java) {
            remote.patch(PendingState(profile.cacheKey, 0, 7, read = true))
        }
        assertEquals(0, server.requestCount)
    }

    @Test fun inboxAccepts65536ChineseRunesEvenWhenUtf8Exceeds65536Bytes() {
        val body = "\u4e2d".repeat(65536)
        enqueue(JSONObject().put("messages", JSONArray().put(messageJson().put("message", body))).toString())
        assertEquals(listOf(sample().copy(body = body)), remote.messages(0))
        request("GET", "/v1/messages?include_hidden=true&before=0")
    }

    @Test fun detailAccepts65536SupplementaryChineseRunesRatherThanUtf16Units() {
        val body = "\uD840\uDC00".repeat(65536)
        enqueue(JSONObject().put("message", messageJson().put("message", body)).toString())
        assertEquals(sample().copy(body = body), remote.message(42))
        request("GET", "/v1/messages/42")
    }

    @Test fun inboxAndDetailRejectBodiesOver65536Runes() {
        for (character in listOf("a", "\u4e2d", "\uD840\uDC00")) {
            val body = character.repeat(65537)
            enqueue(JSONObject().put("messages", JSONArray().put(messageJson().put("message", body))).toString())
            assertThrows(IOException::class.java) { remote.messages(0) }
            request("GET", "/v1/messages?include_hidden=true&before=0")
            enqueue(JSONObject().put("message", messageJson().put("message", body)).toString())
            assertThrows(IOException::class.java) { remote.message(42) }
            request("GET", "/v1/messages/42")
        }
    }

    @Test fun endpointsPreserveHttpErrorsForCacheRevocationAndConflictHandling() {
        val calls: List<Triple<Int, String, () -> Any>> = listOf(
            Triple(401, "/v1/me", { remote.personId() }),
            Triple(500, "/v1/topics", { remote.topics() }),
            Triple(429, "/v1/messages?include_hidden=true&before=0", { remote.messages(0) }),
            Triple(404, "/v1/messages/42", { remote.message(42) }),
            Triple(409, "/v1/messages/42/state", { remote.patch(PendingState(profile.cacheKey, 42, 7, read = true)) }),
        )
        for ((status, path, call) in calls) {
            server.enqueue(MockResponse().setResponseCode(status).setHeader("Content-Type", "application/json")
                .setBody("""{"error":"backend-private-detail"}"""))
            val error = assertThrows(ApiException::class.java) { call() }
            assertEquals(status, error.status)
            assertFalse(error.message.orEmpty().contains("backend-private-detail"))
            request(if (status == 409) "PATCH" else "GET", path)
        }
    }

    private fun enqueue(body: String) {
        server.enqueue(MockResponse().setHeader("Content-Type", "application/json; charset=utf-8").setBody(body))
    }

    private fun request(method: String, path: String): RecordedRequest {
        val sent = server.takeRequest(1, TimeUnit.SECONDS) ?: throw AssertionError("Expected $method $path")
        assertEquals(method, sent.method)
        assertEquals(path, sent.path)
        assertEquals("Bearer synthetic-token", sent.getHeader("Authorization"))
        assertEquals("application/json", sent.getHeader("Accept"))
        assertNull(sent.requestUrl!!.queryParameter("token"))
        return sent
    }

    private fun stateJson() = JSONObject("""{"read":true,"hidden":true,"favorite":false,"version":7}""")

    private fun messageJson(id: Long = 42): JSONObject = JSONObject("""
        {"id":42,"topic_id":3,"title":"CPU alert","message":"High load","priority":8,
         "created_at":"2026-11-12T01:02:03.456Z","state":{"read":true,"hidden":true,"favorite":false,"version":7}}
    """.trimIndent()).put("id", id)

    private fun sample() = InboxMessage(42, 3, "CPU alert", "High load", 8,
        "2026-11-12T01:02:03.456Z", PersonalState(read = true, hidden = true, favorite = false, version = 7))
}
