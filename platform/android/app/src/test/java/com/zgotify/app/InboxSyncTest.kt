package com.zgotify.app

import java.io.IOException
import org.junit.Assert.*
import org.junit.Test

class InboxSyncTest {
    private var now = 100_000L
    private val cache = MemoryMessageCache()
    private val sync = InboxSync(cache) { now }
    private val account = "https://api.example|person:1"
    private val sample = InboxMessage(42, 7, "CPU", "load high", 8, "2026-11-12T00:00:00Z", PersonalState())
    private val remote = FakeInboxRemote(sample)

    private fun seed() { cache.authorize(account, setOf(7), now); cache.upsert(account, listOf(sample)) }

    @Test fun pendingInspectorIncludesHiddenIntentButHonorsAuthorizationExpiry() {
        seed()
        sync.queue(account, 42, StateField.HIDDEN, true)
        assertTrue(sync.cached(account).rows.isEmpty())
        assertTrue(sync.pendingRows(account).single().message.state.hidden)
        now += 24 * 60 * 60 * 1000L + 1
        assertTrue(sync.pendingRows(account).isEmpty())
        assertEquals(1, cache.pending(account).size)
    }

    @Test fun offlineIntentIsVisibleButNotReportedAsServerConfirmed() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        remote.failure = IOException("offline")
        val result = sync.refresh(account, 1, remote)
        assertTrue(result.offline)
        assertTrue(result.rows.single().message.state.read)
        assertEquals(1, result.pendingCount)
        assertFalse(cache.message(account, 42)!!.state.read)
    }

    @Test fun successWritesOnlyTouchedFieldsAndDoesNotReplayAcknowledgedIntent() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        sync.queue(account, 42, StateField.FAVORITE, true)
        sync.refresh(account, 1, remote)
        assertEquals(1, remote.writes.size)
        assertNull(remote.writes.single().hidden)
        assertEquals(0L, remote.writes.single().expectedVersion)
        assertTrue(cache.pending(account).isEmpty())
        assertEquals(1L, cache.message(account, 42)!!.state.version)
        sync.refresh(account, 1, remote)
        assertEquals(1, remote.writes.size)
    }

    @Test fun ambiguousSuccessfulWriteIsAcknowledgedWithoutSecondMutation() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        remote.afterWrite = { throw IOException("response lost") }
        sync.refresh(account, 1, remote)
        assertEquals(1, cache.pending(account).size)
        remote.afterWrite = null
        now += 10_000
        sync.refresh(account, 1, remote)
        assertTrue(cache.pending(account).isEmpty())
        assertEquals(1, remote.writes.size)
        assertTrue(cache.message(account, 42)!!.state.read)
    }

    @Test fun conflictPreservesIntentUntilUserExplicitlyReappliesOnlyTouchedField() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        remote.current = sample.copy(state = PersonalState(favorite = true, version = 2))
        val conflicted = sync.refresh(account, 1, remote)
        assertEquals(1, conflicted.conflictCount)
        assertTrue(cache.pending(account).single().conflict)
        assertTrue(cache.message(account, 42)!!.state.favorite)
        sync.refresh(account, 1, remote, force = true)
        assertTrue(remote.writes.isEmpty())
        sync.resolve(account, 42, keepLocal = true)
        sync.refresh(account, 1, remote)
        assertEquals(2L, remote.writes.single().expectedVersion)
        assertNull(remote.writes.single().favorite)
        assertTrue(remote.current.state.favorite)
        assertTrue(remote.current.state.read)
    }

    @Test fun discardingConflictUsesServerStateWithoutSendingAnyPatch() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        remote.current = sample.copy(state = PersonalState(version = 3))
        sync.refresh(account, 1, remote)
        sync.resolve(account, 42, keepLocal = false)
        assertTrue(cache.pending(account).isEmpty())
        assertFalse(sync.cached(account).rows.single().message.state.read)
        assertTrue(remote.writes.isEmpty())
    }

    @Test fun transientFailureBacksOffWhileManualRefreshCanRetry() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        remote.patchFailure = ApiException(503, "unavailable")
        sync.refresh(account, 1, remote)
        val pending = cache.pending(account).single()
        assertTrue(pending.nextAttemptAt > now)
        assertEquals(1, remote.patchCalls)
        sync.refresh(account, 1, remote)
        assertEquals(1, remote.patchCalls)
        remote.patchFailure = null
        sync.refresh(account, 1, remote, force = true)
        assertEquals(2, remote.patchCalls)
        assertTrue(cache.pending(account).isEmpty())
    }

    @Test fun expiredSessionPurgesOnlyItsOwnIdentityPartition() {
        seed()
        cache.authorize("bob", setOf(7), now)
        cache.upsert("bob", listOf(sample))
        sync.queue(account, 42, StateField.READ, true)
        remote.failure = ApiException(401, "expired")
        assertThrows(ApiException::class.java) { sync.refresh(account, 1, remote) }
        assertTrue(cache.messages(account).isEmpty())
        assertTrue(cache.pending(account).isEmpty())
        assertEquals(1, cache.messages("bob").size)
    }

    @Test fun mismatchedAuthenticatedPersonNeverMergesIntoPreviousIdentity() {
        seed()
        remote.identity = 2
        assertThrows(ApiException::class.java) { sync.refresh(account, 1, remote) }
        assertTrue(cache.messages(account).isEmpty())
        assertTrue(remote.writes.isEmpty())
    }

    @Test fun revokedMembershipPurgesCachedBodyAndQueuedIntentBeforeSending() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        remote.allowed = emptySet()
        val result = sync.refresh(account, 1, remote)
        assertTrue(result.rows.isEmpty())
        assertTrue(cache.pending(account).isEmpty())
        assertEquals(0, remote.patchCalls)
    }

    @Test fun expiredOfflineCacheHidesContentWithoutSilentlyDroppingIntent() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        now += 24 * 60 * 60 * 1000L + 1
        val result = sync.cached(account)
        assertTrue(result.cacheExpired)
        assertTrue(result.rows.isEmpty())
        assertEquals(1, result.pendingCount)
        assertThrows(IllegalStateException::class.java) { sync.queue(account, 42, StateField.HIDDEN, true) }
    }

    @Test fun lostAccessDuringPatchRemovesMessageInsteadOfRetryingForever() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        remote.patchFailure = ApiException(404, "not found")
        remote.listEmpty = true
        sync.refresh(account, 1, remote)
        assertNull(cache.message(account, 42))
        assertTrue(cache.pending(account).isEmpty())
    }

    @Test fun permanentRejectionStaysVisibleAndDoesNotBusyRetry() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        remote.patchFailure = ApiException(400, "invalid")
        sync.refresh(account, 1, remote)
        assertNotNull(cache.pending(account).single().problem)
        sync.refresh(account, 1, remote, force = true)
        assertEquals(1, remote.patchCalls)
    }

    @Test fun acknowledgementCannotRemoveANewerQueuedRevision() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        remote.afterWrite = {
            val old = cache.pending(account).single()
            cache.savePending(old.copy(revision = old.revision + 1, read = false))
        }
        sync.refresh(account, 1, remote)
        assertEquals(false, cache.pending(account).single().read)
    }

    @Test fun lateAcknowledgementCannotRemoveIntentRecreatedAfterAnotherWorkerAcknowledged() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        val inFlight = cache.pending(account).single()
        val replacement = InboxSync(cache) { now }
        remote.afterWrite = {
            // Another worker has reconciled the write before this response arrives.
            cache.upsert(account, listOf(remote.current))
            cache.removePending(account, 42, inFlight.revision)
            replacement.queue(account, 42, StateField.READ, false)
        }

        val result = sync.refresh(account, 1, remote)

        val pending = cache.pending(account).singleOrNull()
        assertNotNull("The old response must not acknowledge the recreated intent", pending)
        assertTrue(pending!!.revision > inFlight.revision)
        assertEquals(false, pending.read)
        assertEquals(1L, pending.expectedVersion)
        assertFalse(result.rows.single().message.state.read)
    }

    @Test fun revisionsAdvanceAcrossSyncInstancesAndPersistedRevisionFloors() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        val original = cache.pending(account).single()
        val persisted = original.copy(revision = original.revision + 10_000)
        cache.savePending(persisted)
        val replacement = InboxSync(cache) { now }

        replacement.queue(account, 42, StateField.FAVORITE, true)
        val updated = cache.pending(account).single()
        assertTrue(updated.revision > persisted.revision)
        cache.removePending(account, 42, updated.revision)

        sync.queue(account, 42, StateField.READ, false)
        assertTrue(cache.pending(account).single().revision > updated.revision)
    }

    @Test fun conflictResolutionUsesTheSameNonReusedRevisionSequence() {
        seed()
        sync.queue(account, 42, StateField.READ, true)
        val original = cache.pending(account).single()
        val conflicted = original.copy(conflict = true, revision = original.revision + 10_000)
        cache.savePending(conflicted)
        cache.upsert(account, listOf(sample.copy(state = PersonalState(version = 5))))

        InboxSync(cache) { now }.resolve(account, 42, keepLocal = true)
        val resolved = cache.pending(account).single()
        assertTrue(resolved.revision > conflicted.revision)
        assertEquals(5L, resolved.expectedVersion)
        assertFalse(resolved.conflict)
        cache.removePending(account, 42, resolved.revision)

        sync.queue(account, 42, StateField.FAVORITE, true)
        assertTrue(cache.pending(account).single().revision > resolved.revision)
    }

    @Test fun cachedPaginationLimitsRawPageBeforeFilteringHiddenMessages() {
        cache.authorize(account, setOf(7), now)
        cache.upsert(account, (201L downTo 1L).map { id ->
            sample.copy(id = id, state = PersonalState(hidden = id >= 102))
        })

        val first = sync.cached(account)
        assertTrue(first.rows.isEmpty())
        assertEquals(102L, first.nextBefore)
        val included = sync.cached(account, includeHidden = true)
        assertEquals((201L downTo 102L).toList(), included.rows.map { it.message.id })
        assertEquals(first.nextBefore, included.nextBefore)

        val second = sync.cached(account, before = first.nextBefore!!)
        assertEquals((101L downTo 2L).toList(), second.rows.map { it.message.id })
        assertEquals(2L, second.nextBefore)
        val last = sync.cached(account, before = second.nextBefore!!)
        assertEquals(listOf(1L), last.rows.map { it.message.id })
        assertEquals(1L, last.nextBefore)
        assertNull(sync.cached(account, before = last.nextBefore!!).nextBefore)
    }

    @Test fun cachedCursorSurvivesPendingHiddenRowsButNotAuthorizationExpiry() {
        seed()
        sync.queue(account, 42, StateField.HIDDEN, true)

        val hidden = sync.cached(account)
        assertTrue(hidden.rows.isEmpty())
        assertEquals(42L, hidden.nextBefore)

        now += 24 * 60 * 60 * 1000L + 1
        val expired = sync.cached(account)
        assertTrue(expired.cacheExpired)
        assertTrue(expired.rows.isEmpty())
        assertNull(expired.nextBefore)
        assertEquals(1, expired.pendingCount)
    }

    @Test fun onlineHiddenPageStillProvidesCursorToOlderVisibleMessages() {
        remote.page = (101L downTo 1L).map { id ->
            sample.copy(id = id, state = PersonalState(hidden = id > 1))
        }

        val first = sync.refresh(account, 1, remote)
        assertFalse(first.offline)
        assertTrue(first.rows.isEmpty())
        assertEquals(2L, first.nextBefore)

        val second = sync.refresh(account, 1, remote, before = first.nextBefore!!)
        assertEquals(listOf(1L), second.rows.map { it.message.id })
        assertEquals(1L, second.nextBefore)
        val end = sync.refresh(account, 1, remote, before = second.nextBefore!!)
        assertTrue(end.rows.isEmpty())
        assertNull(end.nextBefore)
    }

    @Test fun onlinePageRemainsVisibleEvenWhenRetentionImmediatelyPrunesIt() {
        val bounded = object : MessageCache by cache {
            override fun upsert(account: String, messages: List<InboxMessage>) {
                cache.upsert(account, messages)
                val pendingIds = cache.pending(account).map { it.messageId }.toSet()
                cache.messages(account).filter { it.id !in pendingIds }.drop(1000)
                    .forEach { cache.removeMessage(account, it.id) }
            }
        }
        bounded.authorize(account, setOf(7), now)
        bounded.upsert(account, (2000L downTo 1001L).map { sample.copy(id = it) })
        val fetched = (1000L downTo 901L).map { sample.copy(id = it) }
        remote.page = fetched

        val result = InboxSync(bounded) { now }.refresh(account, 1, remote, before = 1001)

        assertNull(cache.message(account, 1000))
        assertEquals(1000, cache.messages(account).size)
        assertFalse(result.offline)
        assertEquals(fetched, result.rows.map { it.message })
        assertEquals(901L, result.nextBefore)
    }

    @Test fun onlineSnapshotContainsOnlyFetchedPageWithPendingOverlay() {
        seed()
        cache.upsert(account, listOf(sample.copy(id = 43), sample.copy(id = 41)))
        sync.queue(account, 42, StateField.READ, true)
        val pending = cache.pending(account).single().copy(nextAttemptAt = now + 60_000)
        cache.savePending(pending)
        remote.current = sample.copy(title = "fresh content", state = PersonalState(favorite = true, version = 2))

        val result = sync.refresh(account, 1, remote)

        assertEquals(listOf(42L), result.rows.map { it.message.id })
        val row = result.rows.single()
        assertEquals("fresh content", row.message.title)
        assertTrue(row.message.state.read)
        assertTrue(row.message.state.favorite)
        assertEquals(pending, row.pending)
        assertEquals(1, result.pendingCount)
        assertEquals(42L, result.nextBefore)
        assertFalse(cache.message(account, 42)!!.state.read)
        assertTrue(remote.writes.isEmpty())
    }

    @Test fun onlinePageDoesNotRegressNewerCachedPersonalState() {
        seed()
        val newer = PersonalState(read = true, favorite = true, version = 8)
        cache.upsert(account, listOf(sample.copy(state = newer)))
        remote.current = sample.copy(title = "fresh content", state = PersonalState(version = 7))

        val result = sync.refresh(account, 1, remote)

        assertEquals(remote.current.copy(state = newer), result.rows.single().message)
        assertEquals(42L, result.nextBefore)
    }

    @Test fun emptyOnlinePageDoesNotFallBackToUnrelatedCachedRows() {
        seed()
        remote.listEmpty = true

        val result = sync.refresh(account, 1, remote)

        assertFalse(result.offline)
        assertTrue(result.rows.isEmpty())
        assertNull(result.nextBefore)
        assertNotNull(cache.message(account, 42))
    }

    @Test fun onlineCursorUsesRawPageEvenWhenUnapprovedTopicsAreFiltered() {
        remote.page = listOf(sample.copy(id = 43), sample.copy(id = 42, topicId = 99))

        val result = sync.refresh(account, 1, remote)

        assertEquals(listOf(43L), result.rows.map { it.message.id })
        assertEquals(42L, result.nextBefore)
        assertNull(cache.message(account, 42))
    }

    @Test fun optimisticHiddenStateFiltersWithoutDeletingServerMessage() {
        seed()
        sync.queue(account, 42, StateField.HIDDEN, true)
        assertTrue(sync.cached(account).rows.isEmpty())
        assertEquals(1, sync.cached(account, includeHidden = true).rows.size)
        assertNotNull(cache.message(account, 42))
        assertFalse(remote.current.state.hidden)
    }
}

private class FakeInboxRemote(var current: InboxMessage) : InboxRemote {
    var identity = 1L
    var allowed = setOf(7L)
    var failure: Exception? = null
    var patchFailure: Exception? = null
    var afterWrite: (() -> Unit)? = null
    var listEmpty = false
    var page: List<InboxMessage>? = null
    var patchCalls = 0
    val writes = mutableListOf<PendingState>()
    override fun personId(): Long { failure?.let { throw it }; return identity }
    override fun topics() = allowed
    override fun messages(before: Long): List<InboxMessage> {
        if (listEmpty) return emptyList()
        val messages = page ?: listOf(current).filter { it.topicId in allowed }
        return messages.filter { before == 0L || it.id < before }.sortedByDescending { it.id }.take(100)
    }
    override fun message(id: Long) = current
    override fun patch(change: PendingState): PersonalState {
        patchCalls++
        patchFailure?.let { throw it }
        if (change.expectedVersion != current.state.version) throw ApiException(409, "conflict")
        val previous = current.state
        current = current.copy(state = PersonalState(change.read ?: previous.read, change.hidden ?: previous.hidden,
            change.favorite ?: previous.favorite, previous.version + 1))
        writes.add(change)
        afterWrite?.invoke()
        return current.state
    }
}

internal class MemoryMessageCache : MessageCache {
    private val items = mutableMapOf<Pair<String, Long>, InboxMessage>()
    private val changes = mutableMapOf<Pair<String, Long>, PendingState>()
    private val times = mutableMapOf<String, Long>()
    override fun messages(account: String, before: Long) = items.filterKeys { it.first == account }
        .values.filter { before == 0L || it.id < before }.sortedByDescending { it.id }
    override fun message(account: String, id: Long) = items[account to id]
    override fun upsert(account: String, messages: List<InboxMessage>) { messages.forEach { next ->
        val old = message(account, next.id)
        items[account to next.id] = if (old != null && old.state.version > next.state.version) next.copy(state = old.state) else next
    } }
    override fun pending(account: String) = changes.filterKeys { it.first == account }.values.toList()
    override fun savePending(change: PendingState) { changes[change.account to change.messageId] = change }
    override fun removePending(account: String, id: Long, revision: Long) {
        if (changes[account to id]?.revision == revision) changes.remove(account to id)
    }
    override fun removeMessage(account: String, id: Long) { items.remove(account to id); changes.remove(account to id) }
    override fun clear(account: String) { messages(account).forEach { removeMessage(account, it.id) }; times.remove(account) }
    override fun authorize(account: String, topicIds: Set<Long>, now: Long) {
        messages(account).filter { it.topicId !in topicIds }.forEach { removeMessage(account, it.id) }; times[account] = now
    }
    override fun authorizedAt(account: String) = times[account]
    override fun <T> transaction(block: () -> T) = block()
}
