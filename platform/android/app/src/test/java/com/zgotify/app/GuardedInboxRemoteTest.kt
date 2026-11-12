package com.zgotify.app

import java.io.IOException
import org.junit.Assert.*
import org.junit.Test

class GuardedInboxRemoteTest {
    private val account = "https://api.example|person:1"
    private val seededAt = 1_000L
    private val now = 100_000L
    private val original = InboxMessage(42, 7, "CPU", "Cached body", 8,
        "2026-11-12T00:00:00Z", PersonalState())
    private val downloaded = original.copy(body = "Downloaded body", state = PersonalState(read = true, version = 1))
    private val pending = PendingState(account, 42, 0, read = true)

    @Test fun invalidBeforeCallSkipsEveryDelegateMethod() {
        for (operation in operations()) {
            val delegate = ProbeRemote(downloaded)
            val guarded = GuardedInboxRemote(delegate) { false }
            assertThrows(operation.name, StaleSessionException::class.java) { operation.call(guarded) }
            assertTrue(delegate.calls.isEmpty())
        }
    }

    @Test fun validCallsPreserveResultsArgumentsAndOriginalFailures() {
        val delegate = ProbeRemote(downloaded)
        val guarded = GuardedInboxRemote(delegate) { true }
        for (operation in operations()) assertEquals(operation.expected, operation.call(guarded))
        assertEquals(listOf("personId", "topics", "messages", "message", "patch"), delegate.calls)
        assertEquals(100L, delegate.before)
        assertEquals(42L, delegate.id)
        assertSame(pending, delegate.change)

        for (failure in listOf(IOException("offline"), ApiException(401, "expired"), IllegalStateException("delegate failure"))) {
            for (operation in operations()) {
                val failing = ProbeRemote(downloaded).apply { onCall = { throw failure } }
                val error = assertThrows(Exception::class.java) {
                    operation.call(GuardedInboxRemote(failing) { true })
                }
                assertSame(failure, error)
            }
        }
    }

    @Test fun invalidAfterSuccessOrFailureReplacesEveryDelegateOutcome() {
        for (operation in operations()) {
            for (failure in listOf<Exception?>(null, ApiException(401, "expired"), IOException("offline"))) {
                var valid = true
                val delegate = ProbeRemote(downloaded).apply {
                    onCall = {
                        valid = false
                        if (failure != null) throw failure
                    }
                }
                val error: Throwable = assertThrows(operation.name, StaleSessionException::class.java) {
                    operation.call(GuardedInboxRemote(delegate) { valid })
                }
                assertTrue(error is RuntimeException)
                assertFalse(error is IOException)
                assertFalse(error is ApiException)
                assertEquals(listOf(operation.name), delegate.calls)
            }
        }
    }

    @Test fun stale401DuringRefreshCannotClearCurrentCacheOrPendingIntent() {
        val cache = seededCache(withPending = true)
        val otherAccount = "https://api.example|person:2"
        cache.authorize(otherAccount, setOf(7), seededAt)
        cache.upsert(otherAccount, listOf(original))
        var valid = true
        val delegate = ProbeRemote(downloaded).apply {
            onCall = {
                valid = false
                throw ApiException(401, "old session expired")
            }
        }
        assertThrows(StaleSessionException::class.java) {
            InboxSync(cache) { now }.refresh(account, 1, GuardedInboxRemote(delegate) { valid })
        }
        assertEquals(listOf("personId"), delegate.calls)
        assertEquals(original, cache.message(account, 42))
        assertEquals(listOf(pending), cache.pending(account))
        assertEquals(seededAt, cache.authorizedAt(account))
        assertEquals(original, cache.message(otherAccount, 42))
    }

    @Test fun lateReadSuccessCannotAuthorizeUpsertOrResolveConflict() {
        for (stage in listOf("topics", "messages", "message")) {
            val cache = seededCache(withPending = stage == "message")
            var valid = true
            val delegate = ProbeRemote(downloaded).apply {
                onCall = { method ->
                    if (stage == "message" && method == "patch") throw ApiException(409, "conflict")
                    if (method == stage) valid = false
                }
            }
            assertThrows(stage, StaleSessionException::class.java) {
                InboxSync(cache) { now }.refresh(account, 1, GuardedInboxRemote(delegate) { valid })
            }
            assertEquals(original, cache.message(account, 42))
            assertEquals(if (stage == "message") listOf(pending) else emptyList<PendingState>(), cache.pending(account))
            // Authorization completed while valid is allowed; a stale topics result is not.
            assertEquals(if (stage == "topics") seededAt else now, cache.authorizedAt(account))
            assertEquals(stage, delegate.calls.last())
        }
    }

    @Test fun latePatchSuccessCannotAcknowledgeOrRetryPendingIntent() {
        val cache = seededCache(withPending = true)
        var valid = true
        val delegate = ProbeRemote(downloaded).apply {
            onCall = { method -> if (method == "patch") valid = false }
        }
        assertThrows(StaleSessionException::class.java) {
            InboxSync(cache) { now }.refresh(account, 1, GuardedInboxRemote(delegate) { valid })
        }
        assertEquals(listOf("personId", "topics", "patch"), delegate.calls)
        assertEquals(original, cache.message(account, 42))
        assertEquals(listOf(pending), cache.pending(account))
        assertEquals(now, cache.authorizedAt(account))
    }

    private fun seededCache(withPending: Boolean): MemoryMessageCache = MemoryMessageCache().apply {
        authorize(account, setOf(7), seededAt)
        upsert(account, listOf(original))
        if (withPending) savePending(pending)
    }

    private fun operations() = listOf(
        Operation("personId", { it.personId() }, 1L),
        Operation("topics", { it.topics() }, setOf(7L)),
        Operation("messages", { it.messages(100) }, listOf(downloaded)),
        Operation("message", { it.message(42) }, downloaded),
        Operation("patch", { it.patch(pending) }, downloaded.state),
    )

    private data class Operation(val name: String, val call: (InboxRemote) -> Any, val expected: Any)

    private class ProbeRemote(private val incoming: InboxMessage) : InboxRemote {
        val calls = mutableListOf<String>()
        var onCall: (String) -> Unit = {}
        var before = -1L
        var id = -1L
        var change: PendingState? = null

        private fun called(method: String) { calls.add(method); onCall(method) }
        override fun personId(): Long { called("personId"); return 1 }
        override fun topics(): Set<Long> { called("topics"); return setOf(incoming.topicId) }
        override fun messages(before: Long): List<InboxMessage> {
            this.before = before
            called("messages")
            return listOf(incoming)
        }
        override fun message(id: Long): InboxMessage {
            this.id = id
            called("message")
            return incoming
        }
        override fun patch(change: PendingState): PersonalState {
            this.change = change
            called("patch")
            return incoming.state
        }
    }
}
