package com.zgotify.app

import android.content.Context
import android.os.Looper
import java.io.File
import java.util.UUID
import java.util.concurrent.Callable
import java.util.concurrent.ExecutionException
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class RoomMessageCacheTest {
    private lateinit var context: Context
    private lateinit var name: String
    private lateinit var cache: RoomMessageCache
    private lateinit var worker: ExecutorService

    @Before
    fun setUp() {
        context = RuntimeEnvironment.getApplication()
        name = "room-message-cache-${UUID.randomUUID()}.db"
        worker = Executors.newSingleThreadExecutor()
        onWorker { cache = RoomMessageCache(context, name) }
    }

    @After
    fun tearDown() {
        try {
            onWorker { cache.close() }
        } finally {
            worker.shutdownNow()
            context.deleteDatabase(name)
        }
    }

    @Test
    fun fileBackedReopenRestoresMessagesPendingAndAuthorization() = onWorker {
        val message = inbox(4_294_967_300L).copy(
            title = "persisted title",
            body = "persisted body",
            priority = 7,
            state = PersonalState(read = true, hidden = true, favorite = true, version = 19),
        )
        val pending = PendingState(
            account = "alpha", messageId = message.id, expectedVersion = 19,
            read = false, hidden = null, favorite = true, revision = 8,
            conflict = true, attempts = 3, nextAttemptAt = 987654321L,
            problem = "version conflict",
        )
        cache.upsert("alpha", listOf(message))
        cache.savePending(pending)
        cache.authorize("alpha", setOf(message.topicId), 123456789L)

        reopen()

        assertEquals(message, cache.message("alpha", message.id))
        assertEquals(listOf(message), cache.messages("alpha"))
        assertEquals(listOf(pending), cache.pending("alpha"))
        assertEquals(123456789L, cache.authorizedAt("alpha"))
        assertNull(cache.authorizedAt("unknown"))
    }

    @Test
    fun identicalMessageIdsAreIndependentAcrossAccounts() = onWorker {
        val alpha = inbox(42).copy(title = "alpha")
        val beta = inbox(42).copy(title = "beta", state = PersonalState(version = 7))
        val alphaPending = PendingState("alpha", 42, 0, read = true)
        val betaPending = PendingState("beta", 42, 7, hidden = true)
        cache.upsert("alpha", listOf(alpha))
        cache.upsert("beta", listOf(beta))
        cache.savePending(alphaPending)
        cache.savePending(betaPending)
        cache.authorize("alpha", setOf(10), 100)
        cache.authorize("beta", setOf(10), 200)

        reopen()

        assertEquals(listOf(alpha), cache.messages("alpha"))
        assertEquals(listOf(beta), cache.messages("beta"))
        assertEquals(listOf(alphaPending), cache.pending("alpha"))
        assertEquals(listOf(betaPending), cache.pending("beta"))
        assertEquals(100L, cache.authorizedAt("alpha"))
        assertEquals(200L, cache.authorizedAt("beta"))
    }

    @Test
    fun authorizationPurgesRevokedMessagesAndPendingIncludingEmptyTopics() = onWorker {
        cache.upsert("alpha", listOf(inbox(1, 10), inbox(2, 20), inbox(3, 20)))
        cache.upsert("beta", listOf(inbox(2, 20)))
        cache.savePending(PendingState("alpha", 1, 0, read = true))
        cache.savePending(PendingState("alpha", 2, 0, hidden = true))
        cache.savePending(PendingState("beta", 2, 0, favorite = true))
        cache.authorize("beta", setOf(20), 90)

        cache.authorize("alpha", setOf(10), 100)

        assertEquals(listOf(1L), cache.messages("alpha").map { it.id })
        assertEquals(listOf(1L), cache.pending("alpha").map { it.messageId })
        assertNull(cache.message("alpha", 2))
        assertNull(cache.message("alpha", 3))
        assertEquals(100L, cache.authorizedAt("alpha"))

        cache.authorize("alpha", emptySet(), 200)
        reopen()

        assertTrue(cache.messages("alpha").isEmpty())
        assertTrue(cache.pending("alpha").isEmpty())
        assertEquals(200L, cache.authorizedAt("alpha"))
        assertEquals(listOf(2L), cache.messages("beta").map { it.id })
        assertEquals(listOf(2L), cache.pending("beta").map { it.messageId })
        assertEquals(90L, cache.authorizedAt("beta"))
    }

    @Test
    fun staleAcknowledgmentCannotRemoveNewerPendingRevision() = onWorker {
        cache.upsert("alpha", listOf(inbox(1)))
        cache.upsert("beta", listOf(inbox(1)))
        cache.savePending(PendingState("alpha", 1, 0, read = true, revision = 1))
        val newer = PendingState("alpha", 1, 1, read = false, favorite = true, revision = 2)
        cache.savePending(newer)
        cache.savePending(PendingState("beta", 1, 0, hidden = true, revision = 2))

        cache.removePending("alpha", 1, 1)
        assertEquals(listOf(newer), cache.pending("alpha"))

        cache.removePending("alpha", 1, 2)
        assertTrue(cache.pending("alpha").isEmpty())
        assertEquals(inbox(1), cache.message("alpha", 1))
        assertEquals(1, cache.pending("beta").size)
    }

    @Test
    fun olderResponseUpdatesContentWithoutRegressingPersonalStateOrPending() = onWorker {
        val state = PersonalState(read = true, hidden = true, favorite = true, version = 9)
        val pending = PendingState("alpha", 1, 9, read = false, revision = 3)
        cache.upsert("alpha", listOf(inbox(1).copy(state = state)))
        cache.savePending(pending)
        val older = inbox(1, 20).copy(
            title = "updated title", body = "updated body", priority = 8,
            createdAt = "2026-11-13T00:00:00Z", state = PersonalState(version = 8),
        )

        cache.upsert("alpha", listOf(older))

        assertEquals(older.copy(state = state), cache.message("alpha", 1))
        assertEquals(listOf(pending), cache.pending("alpha"))

        val newer = older.copy(state = PersonalState(favorite = true, version = 10))
        cache.upsert("alpha", listOf(newer))
        assertEquals(newer, cache.message("alpha", 1))
        val equal = newer.copy(state = PersonalState(read = true, version = 10))
        cache.upsert("alpha", listOf(equal))
        assertEquals(equal, cache.message("alpha", 1))
        assertEquals(listOf(pending), cache.pending("alpha"))
    }

    @Test
    fun savePendingRejectsMessagesMissingFromTheTargetAccount() = onWorker {
        cache.upsert("beta", listOf(inbox(1)))

        assertThrows(IllegalArgumentException::class.java) {
            cache.savePending(PendingState("alpha", 1, 0, read = true))
        }
        assertTrue(cache.pending("alpha").isEmpty())
        assertTrue(cache.pending("beta").isEmpty())
    }

    @Test
    fun removeMessageAlsoRemovesPendingOnlyForTheTargetAccount() = onWorker {
        for (account in listOf("alpha", "beta")) {
            cache.upsert(account, listOf(inbox(1)))
            cache.savePending(PendingState(account, 1, 0, read = true))
        }

        cache.removeMessage("alpha", 1)
        cache.removeMessage("alpha", 1)
        reopen()

        assertNull(cache.message("alpha", 1))
        assertTrue(cache.pending("alpha").isEmpty())
        assertEquals(inbox(1), cache.message("beta", 1))
        assertEquals(1, cache.pending("beta").size)
    }

    @Test
    fun clearRemovesOnlyTheTargetAccountsMessagesPendingAndMetadata() = onWorker {
        for (account in listOf("alpha", "beta")) {
            cache.upsert(account, listOf(inbox(1)))
            cache.savePending(PendingState(account, 1, 0, read = true))
            cache.authorize(account, setOf(10), 100)
        }

        cache.clear("alpha")
        reopen()

        assertTrue(cache.messages("alpha").isEmpty())
        assertTrue(cache.pending("alpha").isEmpty())
        assertNull(cache.authorizedAt("alpha"))
        assertEquals(listOf(inbox(1)), cache.messages("beta"))
        assertEquals(1, cache.pending("beta").size)
        assertEquals(100L, cache.authorizedAt("beta"))
    }

    @Test
    fun failedTransactionRollsBackMessagesPendingAndAuthorization() = onWorker {
        val original = inbox(1)
        val pending = PendingState("alpha", 1, 0, read = true)
        cache.upsert("alpha", listOf(original))
        cache.savePending(pending)
        cache.authorize("alpha", setOf(10), 100)

        assertThrows(IllegalStateException::class.java) {
            cache.transaction {
                cache.authorize("alpha", emptySet(), 200)
                cache.upsert("alpha", listOf(inbox(2)))
                cache.savePending(PendingState("alpha", 2, 0, favorite = true))
                cache.upsert("beta", listOf(inbox(3)))
                cache.authorize("beta", setOf(10), 300)
                error("rollback")
            }
        }

        reopen()
        assertEquals(listOf(original), cache.messages("alpha"))
        assertEquals(listOf(pending), cache.pending("alpha"))
        assertEquals(100L, cache.authorizedAt("alpha"))
        assertTrue(cache.messages("beta").isEmpty())
        assertNull(cache.authorizedAt("beta"))
    }

    @Test
    fun transactionReturnsItsValueAndCommits() = onWorker {
        assertEquals("committed", cache.transaction {
            cache.upsert("alpha", listOf(inbox(1)))
            cache.savePending(PendingState("alpha", 1, 0, hidden = true))
            "committed"
        })
        assertEquals(1, cache.messages("alpha").size)
        assertEquals(1, cache.pending("alpha").size)
        assertNull(cache.transaction<String?> { null })
    }

    @Test
    fun pruningKeepsNewestThousandNonPendingRowsAndNeverEvictsPending() = onWorker {
        cache.upsert("alpha", (1L..1000L).map { inbox(it) })
        cache.savePending(PendingState("alpha", 1, 0, read = true))
        cache.savePending(PendingState("alpha", 2, 0, hidden = true))
        cache.upsert("beta", listOf(inbox(1)))
        cache.upsert("alpha", (1001L..1500L).map { inbox(it) })

        reopen()

        assertEquals((1500L downTo 501L).toList(), cache.messages("alpha").map { it.id })
        assertEquals(listOf(2L, 1L), cache.messages("alpha", before = 501).map { it.id })
        assertEquals(listOf(1L), cache.messages("alpha", before = 2).map { it.id })
        assertTrue(cache.messages("alpha", before = 1).isEmpty())
        assertNull(cache.message("alpha", 500))
        assertEquals(setOf(1L, 2L), cache.pending("alpha").map { it.messageId }.toSet())
        assertEquals(inbox(1), cache.message("beta", 1))

        cache.removePending("alpha", 1, 999)
        assertEquals(inbox(1), cache.message("alpha", 1))
        cache.removePending("alpha", 1, 1)
        assertNull(cache.message("alpha", 1))
        assertEquals(inbox(2), cache.message("alpha", 2))
        assertEquals(listOf(2L), cache.pending("alpha").map { it.messageId })
        assertEquals(1000, cache.messages("alpha").size)
    }

    @Test
    fun fetchedOlderPageStaysActionableWithinTheNonPendingLimit() = onWorker {
        cache.upsert("alpha", listOf(inbox(1)))
        cache.savePending(PendingState("alpha", 1, 0, read = true))
        cache.upsert("alpha", (1001L..2000L).map { inbox(it) })
        cache.upsert("beta", listOf(inbox(1000)))
        cache.authorize("alpha", setOf(10), 1000)

        cache.upsert("alpha", (1000L downTo 901L).map { inbox(it) })
        reopen()

        val retained = (2000L downTo 1101L).toList() + (1000L downTo 901L).toList()
        assertEquals(retained, cache.messages("alpha").map { it.id })
        assertEquals(listOf(1L), cache.messages("alpha", before = 901).map { it.id })
        assertNull(cache.message("alpha", 1100))
        assertEquals(listOf(1L), cache.pending("alpha").map { it.messageId })
        assertEquals(inbox(1000), cache.message("beta", 1000))

        InboxSync(cache) { 1000L }.queue("alpha", 950, StateField.FAVORITE, true)
        assertEquals(true, cache.pending("alpha").single { it.messageId == 950L }.favorite)
        cache.upsert("alpha", (3000L..3099L).map { inbox(it) })
        assertEquals(setOf(1L, 950L), cache.pending("alpha").map { it.messageId }.toSet())
        assertEquals(inbox(950), cache.message("alpha", 950))
    }

    @Test
    fun oversizedUpsertProtectsOnlyItsHundredHighestIds() = onWorker {
        cache.upsert("alpha", (5001L..6000L).map { inbox(it) })

        cache.upsert("alpha", (1L..1500L).map { inbox(it) })

        val retained = (6000L downTo 5101L).toList() + (1500L downTo 1401L).toList()
        assertEquals(retained, cache.messages("alpha").map { it.id })
        assertTrue(cache.messages("alpha", before = 1401).isEmpty())
        assertNull(cache.message("alpha", 1400))
        assertNull(cache.message("alpha", 5100))
    }

    @Test
    fun pendingRowsCanExceedTheMessagePageLimit() = onWorker {
        cache.upsert("alpha", (1L..1000L).map { inbox(it) })
        cache.transaction {
            for (id in 1L..1000L) {
                cache.savePending(PendingState("alpha", id, 0, read = true))
            }
            for (id in 1001L..1002L) {
                cache.upsert("alpha", listOf(inbox(id)))
                cache.savePending(PendingState("alpha", id, 0, favorite = true))
            }
        }

        reopen()

        assertEquals(1002, cache.pending("alpha").size)
        assertEquals((1002L downTo 3L).toList(), cache.messages("alpha").map { it.id })
        assertEquals(listOf(2L, 1L), cache.messages("alpha", before = 3).map { it.id })
    }

    @Test
    fun markdownContentTypeSurvivesReopenAndUpdatesWithoutChangingOtherAccountsOrQueuedState() = onWorker {
        val state = PersonalState(read = true, hidden = true, favorite = true, version = 9)
        val markdown = inbox(42).copy(body = "# Title\n\n**Markdown**", state = state,
            contentType = "text/markdown")
        val plain = inbox(42).copy(body = "**Plain text**")
        val pending = PendingState("alpha", 42, 9, read = false, revision = 3)
        cache.upsert("alpha", listOf(markdown))
        cache.upsert("beta", listOf(plain))
        cache.savePending(pending)

        reopen()

        assertEquals(markdown, cache.message("alpha", 42))
        assertEquals(listOf(markdown), cache.messages("alpha"))
        assertEquals(plain, cache.message("beta", 42))
        assertEquals("text/plain", cache.message("beta", 42)!!.contentType)
        assertEquals(listOf(pending), cache.pending("alpha"))

        cache.upsert("alpha", listOf(plain))
        reopen()

        assertEquals(plain.copy(state = state), cache.message("alpha", 42))
        assertEquals(listOf(pending), cache.pending("alpha"))
        assertEquals(plain, cache.message("beta", 42))

        cache.upsert("alpha", listOf(markdown.copy(state = PersonalState(version = 8))))
        reopen()

        assertEquals(markdown, cache.message("alpha", 42))
        assertEquals(listOf(pending), cache.pending("alpha"))
        assertEquals(plain, cache.message("beta", 42))
    }

    @Test
    fun versionOneMigrationPreservesMessagesQueuedStateAuthorizationAndAccountIsolation() = onWorker {
        val alpha = inbox(4_294_967_300L).copy(
            title = "alpha legacy title", body = "**legacy plain text**",
            state = PersonalState(read = true, hidden = true, favorite = true, version = 19),
        )
        val beta = alpha.copy(title = "beta legacy title", state = PersonalState(version = 7))
        val alphaPending = PendingState(
            "alpha", alpha.id, 19, read = false, hidden = null, favorite = true,
            revision = 8, conflict = true, attempts = 3, nextAttemptAt = 987654321L,
            problem = "version conflict",
        )
        val betaPending = PendingState(
            "beta", beta.id, 7, read = null, hidden = false, favorite = null,
            revision = 2, attempts = 1, nextAttemptAt = 123456789L, problem = "retry later",
        )
        createVersionOneDatabase(listOf(
            Triple(alpha, alphaPending, 111L),
            Triple(beta, betaPending, 222L),
        ))
        cache = RoomMessageCache(context, name)

        assertEquals(listOf(alpha), cache.messages("alpha"))
        assertEquals(listOf(beta), cache.messages("beta"))
        assertEquals(listOf(alphaPending), cache.pending("alpha"))
        assertEquals(listOf(betaPending), cache.pending("beta"))
        assertEquals(111L, cache.authorizedAt("alpha"))
        assertEquals(222L, cache.authorizedAt("beta"))
        cache.close()
        context.openOrCreateDatabase(name, Context.MODE_PRIVATE, null).use { database ->
            assertEquals(2, database.version)
            database.rawQuery("SELECT contentType FROM cached_messages ORDER BY account", null).use { cursor ->
                assertEquals(2, cursor.count)
                while (cursor.moveToNext()) assertEquals("text/plain", cursor.getString(0))
            }
            database.rawQuery("PRAGMA foreign_key_check", null).use { cursor ->
                assertEquals(0, cursor.count)
            }
        }
        cache = RoomMessageCache(context, name)

        assertEquals(alpha, cache.message("alpha", alpha.id))
        assertEquals(beta, cache.message("beta", beta.id))
        assertEquals(listOf(alphaPending), cache.pending("alpha"))
        assertEquals(listOf(betaPending), cache.pending("beta"))
        assertEquals(111L, cache.authorizedAt("alpha"))
        assertEquals(222L, cache.authorizedAt("beta"))
        assertNull(cache.message("unknown", alpha.id))
        assertTrue(cache.pending("unknown").isEmpty())
        assertNull(cache.authorizedAt("unknown"))

        val markdown = alpha.copy(body = "# Migrated Markdown", contentType = "text/markdown")
        cache.upsert("alpha", listOf(markdown))
        reopen()

        assertEquals(markdown, cache.message("alpha", alpha.id))
        assertEquals(beta, cache.message("beta", beta.id))
        assertEquals(listOf(alphaPending), cache.pending("alpha"))
        assertEquals(listOf(betaPending), cache.pending("beta"))
        assertEquals(111L, cache.authorizedAt("alpha"))
        assertEquals(222L, cache.authorizedAt("beta"))

        cache.removePending("alpha", alpha.id, alphaPending.revision - 1)
        assertEquals(listOf(alphaPending), cache.pending("alpha"))
        cache.authorize("alpha", emptySet(), 333L)
        reopen()

        assertTrue(cache.messages("alpha").isEmpty())
        assertTrue(cache.pending("alpha").isEmpty())
        assertEquals(333L, cache.authorizedAt("alpha"))
        assertEquals(listOf(beta), cache.messages("beta"))
        assertEquals(listOf(betaPending), cache.pending("beta"))
        assertEquals(222L, cache.authorizedAt("beta"))
    }

    @Test
    fun unsupportedSchemaVersionFailsWithoutDestructivelyRecreatingData() = onWorker {
        cache.upsert("alpha", listOf(inbox(1)))
        cache.savePending(PendingState("alpha", 1, 0, read = true))
        cache.authorize("alpha", setOf(10), 100)
        cache.close()
        context.openOrCreateDatabase(name, Context.MODE_PRIVATE, null).use { database ->
            assertEquals(2, database.version)
            database.version = 3
        }
        cache = RoomMessageCache(context, name)

        assertThrows(IllegalStateException::class.java) { cache.messages("alpha") }

        context.openOrCreateDatabase(name, Context.MODE_PRIVATE, null).use { database ->
            assertEquals(3, database.version)
            for (table in listOf("cached_messages", "pending_states", "account_authorization")) {
                database.rawQuery("SELECT COUNT(*) FROM $table", null).use { cursor ->
                    assertTrue(cursor.moveToFirst())
                    assertEquals(1L, cursor.getLong(0))
                }
            }
        }
    }

    @Test
    fun mainThreadQueriesAreNotAllowed() {
        onWorker { cache.upsert("alpha", listOf(inbox(1))) }
        assertSame(Looper.getMainLooper(), Looper.myLooper())
        assertThrows(IllegalStateException::class.java) { cache.messages("alpha") }
    }

    private fun inbox(id: Long, topicId: Long = 10) = InboxMessage(
        id = id, topicId = topicId, title = "title $id", body = "body $id",
        priority = 5, createdAt = "2026-11-12T00:00:00Z", state = PersonalState(),
    )

    private fun createVersionOneDatabase(rows: List<Triple<InboxMessage, PendingState, Long>>) {
        cache.close()
        val schemaPath = "schemas/com.zgotify.app.RoomMessageDatabase/1.json"
        val schemaFile = listOf(File(schemaPath), File("app/$schemaPath"), File("android/app/$schemaPath"))
            .first { it.isFile }
        val schema = JSONObject(schemaFile.readText()).getJSONObject("database")
        assertEquals(1, schema.getInt("version"))
        context.openOrCreateDatabase(name, Context.MODE_PRIVATE, null).use { database ->
            database.setForeignKeyConstraintsEnabled(true)
            val entities = schema.getJSONArray("entities")
            for (index in 0 until entities.length()) {
                val entity = entities.getJSONObject(index)
                database.execSQL(entity.getString("createSql")
                    .replace("\u0024{TABLE_NAME}", entity.getString("tableName")))
            }
            val setup = schema.getJSONArray("setupQueries")
            for (index in 0 until setup.length()) database.execSQL(setup.getString(index))
            for ((message, pending, authorizedAt) in rows) {
                database.execSQL("""
                    INSERT INTO cached_messages
                    (account, messageId, topicId, title, body, priority, createdAt,
                     stateRead, stateHidden, stateFavorite, stateVersion)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """.trimIndent(), arrayOf<Any?>(
                    pending.account, message.id, message.topicId, message.title, message.body,
                    message.priority, message.createdAt, message.state.read.toSql(),
                    message.state.hidden.toSql(), message.state.favorite.toSql(), message.state.version,
                ))
                database.execSQL("""
                    INSERT INTO pending_states
                    (account, messageId, expectedVersion, read, hidden, favorite, revision,
                     conflict, attempts, nextAttemptAt, problem)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """.trimIndent(), arrayOf<Any?>(
                    pending.account, pending.messageId, pending.expectedVersion, pending.read.toSql(),
                    pending.hidden.toSql(), pending.favorite.toSql(), pending.revision,
                    pending.conflict.toSql(), pending.attempts, pending.nextAttemptAt, pending.problem,
                ))
                database.execSQL("INSERT INTO account_authorization (account, authorizedAt) VALUES (?, ?)",
                    arrayOf<Any?>(pending.account, authorizedAt))
            }
            database.version = 1
        }
        assertTrue(context.getDatabasePath(name).isFile)
    }

    private fun Boolean?.toSql(): Int? = this?.let { if (it) 1 else 0 }

    private fun reopen() {
        cache.close()
        assertTrue(context.getDatabasePath(name).isFile)
        cache = RoomMessageCache(context, name)
    }

    private fun <T> onWorker(block: () -> T): T = try {
        worker.submit(Callable { block() }).get(60, TimeUnit.SECONDS)
    } catch (failure: ExecutionException) {
        throw failure.cause ?: failure
    }
}
