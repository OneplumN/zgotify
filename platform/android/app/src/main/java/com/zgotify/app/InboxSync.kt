package com.zgotify.app

import java.io.IOException
import java.util.concurrent.atomic.AtomicLong

enum class StateField { READ, HIDDEN, FAVORITE }
data class InboxRow(val message: InboxMessage, val pending: PendingState?)
data class InboxSnapshot(
    val rows: List<InboxRow>, val pendingCount: Int, val conflictCount: Int,
    val cacheExpired: Boolean, val offline: Boolean = false, val notice: String? = null,
    val nextBefore: Long? = null
)

interface InboxRemote {
    fun personId(): Long
    fun topics(): Set<Long>
    fun messages(before: Long): List<InboxMessage>
    fun message(id: Long): InboxMessage
    fun patch(change: PendingState): PersonalState
}

class InboxSync(private val cache: MessageCache, private val clock: () -> Long = System::currentTimeMillis) {
    private val ttl = 24 * 60 * 60 * 1000L

    private fun fresh(account: String): Boolean {
        val checked = cache.authorizedAt(account) ?: return false
        val age = clock() - checked
        return age in 0..ttl
    }

    fun cached(account: String, includeHidden: Boolean = false, before: Long = 0): InboxSnapshot = cache.transaction {
        val valid = fresh(account)
        val page = if (valid) cache.messages(account, before).take(100) else emptyList()
        snapshot(page, cache.pending(account), includeHidden, cacheExpired = !valid)
    }

    fun queue(account: String, id: Long, field: StateField, value: Boolean) = cache.transaction {
        check(fresh(account)) { "缓存授权已过期，请先联网刷新" }
        val message = cache.message(account, id) ?: error("消息已不在本机缓存中")
        val old = cache.pending(account).find { it.messageId == id }
        check(old?.conflict != true && old?.problem == null) { "请先处理该消息的同步冲突" }
        val next = (old ?: PendingState(account, id, message.state.version)).copy(
            revision = nextRevision(old?.revision ?: 0), attempts = 0, nextAttemptAt = 0)
        cache.savePending(when (field) {
            StateField.READ -> next.copy(read = value)
            StateField.HIDDEN -> next.copy(hidden = value)
            StateField.FAVORITE -> next.copy(favorite = value)
        })
    }

    fun pendingRows(account: String): List<InboxRow> = cache.transaction {
        if (!fresh(account)) return@transaction emptyList()
        cache.pending(account).mapNotNull { change ->
            cache.message(account, change.messageId)?.let { overlay(it, change) }
        }
    }

    fun resolve(account: String, id: Long, keepLocal: Boolean) = cache.transaction {
        val change = cache.pending(account).find { it.messageId == id } ?: return@transaction
        if (!keepLocal) cache.removePending(account, id, change.revision)
        else {
            check(fresh(account)) { "请先联网获取最新状态" }
            val message = cache.message(account, id) ?: error("消息已不可访问")
            cache.savePending(change.copy(expectedVersion = message.state.version, revision = nextRevision(change.revision),
                conflict = false, problem = null, attempts = 0, nextAttemptAt = 0))
        }
    }

    fun refresh(account: String, personId: Long, remote: InboxRemote,
                includeHidden: Boolean = false, before: Long = 0, force: Boolean = false): InboxSnapshot {
        require(personId > 0 && account.isNotBlank())
        try {
            if (remote.personId() != personId) throw ApiException(401, "登录身份已变化，请重新登录")
            val topics = remote.topics()
            cache.authorize(account, topics, clock())
            for (change in cache.pending(account).filter {
                !it.conflict && it.problem == null && (force || it.nextAttemptAt <= clock())
            }.take(20)) send(change, remote)
            val page = remote.messages(before)
            return cache.transaction {
                // Keep the fetched page/cursor independent of cache retention and visibility filters.
                val authorizedPage = page.filter { it.topicId in topics }
                cache.upsert(account, authorizedPage)
                val currentPage = authorizedPage.map { message ->
                    val cached = cache.message(account, message.id)
                    if (cached != null && cached.state.version > message.state.version) {
                        message.copy(state = cached.state)
                    } else message
                }
                snapshot(currentPage, cache.pending(account), includeHidden,
                    cacheExpired = !fresh(account), nextBefore = page.lastOrNull()?.id)
            }
        } catch (error: ApiException) {
            if (error.status == 401) { cache.clear(account); throw error }
            if (error.status != 429 && error.status < 500) throw error
        } catch (_: IOException) {
            // Keep local intent durable; the next foreground/manual sync can retry.
        }
        return cached(account, includeHidden, before).copy(offline = true, notice = "连接暂不可用，显示本机缓存")
    }

    private fun snapshot(page: List<InboxMessage>, changes: List<PendingState>, includeHidden: Boolean,
                         cacheExpired: Boolean = false, nextBefore: Long? = page.lastOrNull()?.id): InboxSnapshot {
        val pending = changes.associateBy { it.messageId }
        val rows = if (cacheExpired) emptyList() else page.map { overlay(it, pending[it.id]) }
            .filter { includeHidden || !it.message.state.hidden }
        return InboxSnapshot(rows, changes.size, changes.count { it.conflict || it.problem != null },
            cacheExpired, nextBefore = if (cacheExpired) null else nextBefore)
    }

    private fun overlay(message: InboxMessage, pending: PendingState?): InboxRow {
        val state = message.state
        return InboxRow(message.copy(state = state.copy(read = pending?.read ?: state.read,
            hidden = pending?.hidden ?: state.hidden, favorite = pending?.favorite ?: state.favorite)), pending)
    }

    private fun send(change: PendingState, remote: InboxRemote) {
        try {
            val state = remote.patch(change)
            if (state.version != change.expectedVersion + 1) throw IOException("Invalid state version")
            acknowledge(change, state)
        } catch (error: ApiException) {
            when {
                error.status == 401 -> throw error
                error.status == 404 -> cache.removeMessage(change.account, change.messageId)
                error.status == 409 -> reconcile(change, remote)
                error.status == 429 || error.status >= 500 -> { defer(change); throw error }
                else -> updateIfCurrent(change) { it.copy(problem = "服务器拒绝了此修改") }
            }
        } catch (error: IOException) { defer(change); throw error }
    }

    private fun reconcile(change: PendingState, remote: InboxRemote) {
        val message = try { remote.message(change.messageId) } catch (error: ApiException) {
            if (error.status == 404) { cache.removeMessage(change.account, change.messageId); return }
            if (error.status == 429 || error.status >= 500) defer(change)
            throw error
        } catch (error: IOException) { defer(change); throw error }
        if (message.id != change.messageId) throw IOException("Invalid message identity")
        cache.transaction {
            cache.upsert(change.account, listOf(message))
            val state = message.state
            if ((change.read == null || change.read == state.read) &&
                (change.hidden == null || change.hidden == state.hidden) &&
                (change.favorite == null || change.favorite == state.favorite)) {
                cache.removePending(change.account, change.messageId, change.revision)
            } else updateIfCurrent(change) { it.copy(conflict = true) }
        }
    }

    private fun acknowledge(change: PendingState, state: PersonalState) = cache.transaction {
        cache.message(change.account, change.messageId)?.let { cache.upsert(change.account, listOf(it.copy(state = state))) }
        cache.removePending(change.account, change.messageId, change.revision)
    }

    private fun defer(change: PendingState) = updateIfCurrent(change) {
        val attempt = (it.attempts + 1).coerceAtMost(20)
        val delay = (5_000L shl (attempt - 1).coerceAtMost(6)).coerceAtMost(300_000L)
        it.copy(attempts = attempt, nextAttemptAt = clock() + delay)
    }

    private fun updateIfCurrent(change: PendingState, update: (PendingState) -> PendingState) = cache.transaction {
        cache.pending(change.account).find { it.messageId == change.messageId && it.revision == change.revision }
            ?.let { cache.savePending(update(it)) }
    }

    private companion object {
        val revisions = AtomicLong()

        fun nextRevision(previous: Long): Long = revisions.updateAndGet { current ->
            val highest = maxOf(current, previous)
            check(highest < Long.MAX_VALUE) { "Pending revision exhausted" }
            highest + 1
        }
    }
}
