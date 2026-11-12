package com.zgotify.app

import java.io.IOException
import org.json.JSONObject

class ApiInboxRemote(private val profile: Profile) : InboxRemote {
    override fun personId(): Long = parse { positive(Api.request(profile, "GET", "/v1/me").getJSONObject("person"), "id") }

    override fun topics(): Set<Long> = parse {
        val topics = Api.request(profile, "GET", "/v1/topics").getJSONArray("topics")
        val ids = (0 until topics.length()).map { positive(topics.getJSONObject(it), "id") }
        require(ids.toSet().size == ids.size)
        ids.toSet()
    }

    override fun messages(before: Long): List<InboxMessage> = parse {
        require(before >= 0)
        val array = Api.request(profile, "GET", "/v1/messages?include_hidden=true&before=$before").getJSONArray("messages")
        require(array.length() <= 100)
        val messages = (0 until array.length()).map { readMessage(array.getJSONObject(it)) }
        require(messages.zipWithNext().all { (a, b) -> a.id > b.id })
        require(before == 0L || messages.all { it.id < before })
        messages
    }

    override fun message(id: Long): InboxMessage = parse {
        val message = readMessage(Api.request(profile, "GET", "/v1/messages/$id").getJSONObject("message"))
        require(message.id == id)
        message
    }

    override fun patch(change: PendingState): PersonalState = parse {
        require(change.account == profile.cacheKey && change.messageId > 0)
        val body = JSONObject().put("expected_version", change.expectedVersion)
        change.read?.let { body.put("read", it) }
        change.hidden?.let { body.put("hidden", it) }
        change.favorite?.let { body.put("favorite", it) }
        state(Api.request(profile, "PATCH", "/v1/messages/${change.messageId}/state", body))
    }

    private fun readMessage(json: JSONObject): InboxMessage {
        val title = string(json, "title")
        val body = string(json, "message")
        val priority = number(json, "priority")
        require(title.length <= 1024 && body.codePointCount(0, body.length) <= 65536 && priority in 0..10)
        return InboxMessage(positive(json, "id"), positive(json, "topic_id"), title, body, priority.toInt(),
            string(json, "created_at"), state(json.getJSONObject("state")),
            contentType = if ((json.opt("content_type") as? String) == "text/markdown") "text/markdown" else "text/plain")
    }

    private fun state(json: JSONObject): PersonalState {
        val version = number(json, "version")
        require(version >= 0)
        return PersonalState(boolean(json, "read"), boolean(json, "hidden"), boolean(json, "favorite"), version)
    }

    private fun number(json: JSONObject, key: String): Long {
        val value = json.get(key)
        require(value is Int || value is Long)
        return (value as Number).toLong()
    }
    private fun positive(json: JSONObject, key: String) = number(json, key).also { require(it > 0) }
    private fun boolean(json: JSONObject, key: String) = (json.get(key) as? Boolean) ?: throw IOException("Invalid state")
    private fun string(json: JSONObject, key: String) = (json.get(key) as? String) ?: throw IOException("Invalid message")
    private fun <T> parse(block: () -> T): T = try { block() } catch (error: ApiException) { throw error }
        catch (error: IOException) { throw error } catch (_: Exception) { throw IOException("Invalid server response") }
}
