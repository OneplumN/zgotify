package com.zgotify.app

import org.json.JSONObject
import java.io.IOException
import java.net.URI
import java.util.concurrent.TimeUnit
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

data class Profile(val server: String, val username: String, val token: String, val personId: Long = 0) {
    val key: String get() = "$server|$username"
    val cacheKey: String get() {
        require(personId > 0) { "Authenticated person ID is required" }
        return "$server|person:$personId"
    }
}

class ApiException(val status: Int, message: String) : Exception(message)

object Api {
    private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(4, TimeUnit.SECONDS).readTimeout(8, TimeUnit.SECONDS).callTimeout(10, TimeUnit.SECONDS).build()
    fun normalizeServer(value: String): String {
        val uri = URI(value.trim())
        require(uri.host != null && uri.rawUserInfo == null && uri.rawQuery == null && uri.rawFragment == null) {
            "Invalid server address"
        }
        require(uri.path.isNullOrEmpty() || uri.path == "/") { "Use the dedicated API host without a path" }
        val local = uri.host in listOf("10.0.2.2", "localhost", "127.0.0.1")
        require(uri.scheme == "https" || (BuildConfig.DEBUG && uri.scheme == "http" && local)) {
            "HTTPS is required (internal emulator builds also allow local HTTP)"
        }
        return URI(uri.scheme, null, uri.host.lowercase(), uri.port, null, null, null).toASCIIString()
    }

    fun login(candidate: Profile, body: JSONObject): Profile {
        val response = request(candidate.copy(token = ""), "POST", "/v1/login", body)
        val token = response.opt("token") as? String
        try {
            require(!token.isNullOrEmpty())
            val person = response.getJSONObject("person")
            val id = person.get("id")
            require(id is Int || id is Long)
            val personId = (id as Number).toLong()
            val username = person.get("username") as? String
            require(personId > 0 && !username.isNullOrBlank())
            return candidate.copy(username = username, token = token, personId = personId)
        } catch (_: Exception) {
            if (!token.isNullOrEmpty()) runCatching { request(candidate.copy(token = token), "DELETE", "/v1/session") }
            throw IOException("Invalid login response")
        }
    }

    fun request(profile: Profile, method: String, path: String, body: JSONObject? = null): JSONObject {
        val request = Request.Builder().url(profile.server + path).header("Accept", "application/json")
        if (profile.token.isNotEmpty()) request.header("Authorization", "Bearer ${profile.token}")
        val requestBody = body?.toString()?.toRequestBody("application/json; charset=utf-8".toMediaType())
        request.method(method, requestBody)
        client.newCall(request.build()).execute().use { response ->
            val status = response.code
            if (status !in 200..299) throw ApiException(status, "HTTP $status")
            if (status == 204) return JSONObject()
            val source = response.body?.source() ?: throw IOException("Missing response")
            val limit = 8L * 1024 * 1024
            source.request(limit + 1)
            if (source.buffer.size > limit) throw IOException("Response too large")
            return try { JSONObject(source.readUtf8()) } catch (_: Exception) { throw IOException("Invalid JSON response") }
        }
    }
}
