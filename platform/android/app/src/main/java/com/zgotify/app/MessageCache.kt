package com.zgotify.app

data class PersonalState(
    val read: Boolean = false,
    val hidden: Boolean = false,
    val favorite: Boolean = false,
    val version: Long = 0,
)

data class InboxMessage(
    val id: Long,
    val topicId: Long,
    val title: String,
    val body: String,
    val priority: Int,
    val createdAt: String,
    val state: PersonalState,
)

data class PendingState(
    val account: String,
    val messageId: Long,
    val expectedVersion: Long,
    val read: Boolean? = null,
    val hidden: Boolean? = null,
    val favorite: Boolean? = null,
    val revision: Long = 1,
    val conflict: Boolean = false,
    val attempts: Int = 0,
    val nextAttemptAt: Long = 0,
    val problem: String? = null,
)

interface MessageCache {
    fun messages(account: String, before: Long = 0): List<InboxMessage>
    fun message(account: String, id: Long): InboxMessage?
    fun upsert(account: String, messages: List<InboxMessage>)
    fun pending(account: String): List<PendingState>
    fun savePending(change: PendingState)
    fun removePending(account: String, id: Long, revision: Long)
    fun removeMessage(account: String, id: Long)
    fun clear(account: String)
    fun authorize(account: String, topicIds: Set<Long>, now: Long)
    fun authorizedAt(account: String): Long?
    fun <T> transaction(block: () -> T): T
}
