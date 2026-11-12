package com.zgotify.app

import android.content.Context
import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Database
import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.Upsert
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase
import java.io.Closeable
import java.util.concurrent.Callable

class RoomMessageCache(
    context: Context,
    name: String = "zgotify-inbox.db",
) : MessageCache, Closeable {
    private val database = Room.databaseBuilder(
        context.applicationContext, RoomMessageDatabase::class.java, name,
    ).addMigrations(RoomMessageDatabase.MIGRATION_1_2).build()
    private val dao = database.cacheDao()

    override fun messages(account: String, before: Long): List<InboxMessage> =
        dao.messages(account, before).map { it.toMessage() }

    override fun message(account: String, id: Long): InboxMessage? =
        dao.message(account, id)?.toMessage()

    override fun upsert(account: String, messages: List<InboxMessage>) {
        transaction {
            for (message in messages) {
                val cached = dao.message(account, message.id)
                val state = if (cached != null && cached.stateVersion > message.state.version) {
                    cached.toMessage().state
                } else {
                    message.state
                }
                dao.upsertMessage(CachedMessageEntity.from(account, message.copy(state = state)))
            }
            val protectedIds = messages.map { it.id }.distinct().sortedDescending().take(100)
            dao.prune(account, protectedIds)
        }
    }

    override fun pending(account: String): List<PendingState> =
        dao.pending(account).map { it.toPending() }

    override fun savePending(change: PendingState) {
        transaction {
            require(dao.message(change.account, change.messageId) != null) {
                "Pending state requires a cached message in the same account"
            }
            dao.upsertPending(PendingStateEntity.from(change))
        }
    }

    override fun removePending(account: String, id: Long, revision: Long) {
        transaction {
            dao.removePending(account, id, revision)
            dao.prune(account)
        }
    }

    override fun removeMessage(account: String, id: Long) {
        dao.removeMessage(account, id)
    }

    override fun clear(account: String) {
        transaction {
            dao.clearMessages(account)
            dao.clearAuthorization(account)
        }
    }

    override fun authorize(account: String, topicIds: Set<Long>, now: Long) {
        transaction {
            // Bind one revoked topic at a time so large grants cannot exceed SQLite's bind limit.
            for (topicId in dao.topicIds(account)) {
                if (topicId !in topicIds) dao.removeTopic(account, topicId)
            }
            dao.upsertAuthorization(AuthorizationEntity(account, now))
        }
    }

    override fun authorizedAt(account: String): Long? = dao.authorizedAt(account)

    override fun <T> transaction(block: () -> T): T =
        database.runInTransaction(Callable { block() })

    override fun close() {
        database.close()
    }
}

@Entity(tableName = "cached_messages", primaryKeys = ["account", "messageId"])
internal data class CachedMessageEntity(
    val account: String,
    val messageId: Long,
    val topicId: Long,
    val title: String,
    val body: String,
    val priority: Int,
    val createdAt: String,
    val stateRead: Boolean,
    val stateHidden: Boolean,
    val stateFavorite: Boolean,
    val stateVersion: Long,
    @ColumnInfo(defaultValue = "'text/plain'") val contentType: String = "text/plain",
) {
    fun toMessage() = InboxMessage(
        id = messageId, topicId = topicId, title = title, body = body,
        priority = priority, createdAt = createdAt,
        state = PersonalState(stateRead, stateHidden, stateFavorite, stateVersion),
        contentType = contentType,
    )

    companion object {
        fun from(account: String, message: InboxMessage) = CachedMessageEntity(
            account = account, messageId = message.id, topicId = message.topicId,
            title = message.title, body = message.body, priority = message.priority,
            createdAt = message.createdAt, stateRead = message.state.read,
            stateHidden = message.state.hidden, stateFavorite = message.state.favorite,
            stateVersion = message.state.version,
            contentType = message.contentType,
        )
    }
}

@Entity(
    tableName = "pending_states",
    primaryKeys = ["account", "messageId"],
    foreignKeys = [ForeignKey(
        entity = CachedMessageEntity::class,
        parentColumns = ["account", "messageId"],
        childColumns = ["account", "messageId"],
        onDelete = ForeignKey.CASCADE,
    )],
)
internal data class PendingStateEntity(
    val account: String,
    val messageId: Long,
    val expectedVersion: Long,
    val read: Boolean?,
    val hidden: Boolean?,
    val favorite: Boolean?,
    val revision: Long,
    val conflict: Boolean,
    val attempts: Int,
    val nextAttemptAt: Long,
    val problem: String?,
) {
    fun toPending() = PendingState(
        account = account, messageId = messageId, expectedVersion = expectedVersion,
        read = read, hidden = hidden, favorite = favorite, revision = revision,
        conflict = conflict, attempts = attempts, nextAttemptAt = nextAttemptAt, problem = problem,
    )

    companion object {
        fun from(change: PendingState) = PendingStateEntity(
            account = change.account, messageId = change.messageId,
            expectedVersion = change.expectedVersion, read = change.read,
            hidden = change.hidden, favorite = change.favorite, revision = change.revision,
            conflict = change.conflict, attempts = change.attempts,
            nextAttemptAt = change.nextAttemptAt, problem = change.problem,
        )
    }
}

@Entity(tableName = "account_authorization")
internal data class AuthorizationEntity(
    @PrimaryKey val account: String,
    val authorizedAt: Long,
)

@Dao
internal interface MessageCacheDao {
    @Query("""
        SELECT * FROM cached_messages
        WHERE account = :account AND (:before = 0 OR messageId < :before)
        ORDER BY messageId DESC LIMIT 1000
    """)
    fun messages(account: String, before: Long): List<CachedMessageEntity>

    @Query("SELECT * FROM cached_messages WHERE account = :account AND messageId = :id")
    fun message(account: String, id: Long): CachedMessageEntity?

    @Upsert
    fun upsertMessage(message: CachedMessageEntity)

    @Query("SELECT * FROM pending_states WHERE account = :account ORDER BY messageId ASC")
    fun pending(account: String): List<PendingStateEntity>

    @Upsert
    fun upsertPending(change: PendingStateEntity)

    @Query("DELETE FROM pending_states WHERE account = :account AND messageId = :id AND revision = :revision")
    fun removePending(account: String, id: Long, revision: Long)

    @Query("DELETE FROM cached_messages WHERE account = :account AND messageId = :id")
    fun removeMessage(account: String, id: Long)

    @Query("DELETE FROM cached_messages WHERE account = :account")
    fun clearMessages(account: String)

    @Query("SELECT DISTINCT topicId FROM cached_messages WHERE account = :account")
    fun topicIds(account: String): List<Long>

    @Query("DELETE FROM cached_messages WHERE account = :account AND topicId = :topicId")
    fun removeTopic(account: String, topicId: Long)

    @Query("""
        DELETE FROM cached_messages
        WHERE account = :account AND messageId IN (
            SELECT m.messageId FROM cached_messages AS m
            WHERE m.account = :account AND NOT EXISTS (
                SELECT 1 FROM pending_states AS p
                WHERE p.account = m.account AND p.messageId = m.messageId
            )
            ORDER BY CASE WHEN m.messageId IN (:protectedIds) THEN 0 ELSE 1 END,
                m.messageId DESC LIMIT -1 OFFSET 1000
        )
    """)
    fun prune(account: String, protectedIds: List<Long> = emptyList())

    @Upsert
    fun upsertAuthorization(authorization: AuthorizationEntity)

    @Query("SELECT authorizedAt FROM account_authorization WHERE account = :account")
    fun authorizedAt(account: String): Long?

    @Query("DELETE FROM account_authorization WHERE account = :account")
    fun clearAuthorization(account: String)
}

@Database(
    entities = [CachedMessageEntity::class, PendingStateEntity::class, AuthorizationEntity::class],
    version = 2,
    exportSchema = true,
)
internal abstract class RoomMessageDatabase : RoomDatabase() {
    abstract fun cacheDao(): MessageCacheDao

    companion object {
        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE cached_messages ADD COLUMN contentType TEXT NOT NULL DEFAULT 'text/plain'")
            }
        }
    }
}
