package com.zgotify.app

class StaleSessionException : RuntimeException("Session is no longer current")

class GuardedInboxRemote(
    private val delegate: InboxRemote,
    private val valid: () -> Boolean,
) : InboxRemote {
    override fun personId(): Long = guarded { delegate.personId() }
    override fun topics(): Set<Long> = guarded { delegate.topics() }
    override fun messages(before: Long): List<InboxMessage> = guarded { delegate.messages(before) }
    override fun message(id: Long): InboxMessage = guarded { delegate.message(id) }
    override fun patch(change: PendingState): PersonalState = guarded { delegate.patch(change) }

    private inline fun <T> guarded(call: () -> T): T {
        if (!valid()) throw StaleSessionException()
        try {
            return call()
        } finally {
            // Replace stale failures before InboxSync can clear caches or schedule retries.
            if (!valid()) throw StaleSessionException()
        }
    }
}
