package com.zgotify.app

import org.json.JSONObject
import java.net.URI
import java.util.concurrent.TimeUnit
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

data class Profile(val server: String, val username: String, val token: String) {
    val key: String get() = "$server|$username"
}

class ApiException(val status: Int, message: String) : Exception(message)

object Api {
    private val client = OkHttpClient.Builder().followRedirects(false).followSslRedirects(false)
        .connectTimeout(10, TimeUnit.SECONDS).readTimeout(20, TimeUnit.SECONDS).callTimeout(25, TimeUnit.SECONDS).build()
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

    fun request(profile: Profile, method: String, path: String, body: JSONObject? = null): JSONObject {
        val request = Request.Builder().url(profile.server + path).header("Accept", "application/json")
        if (profile.token.isNotEmpty()) request.header("Authorization", "Bearer ${profile.token}")
        val requestBody = body?.toString()?.toRequestBody("application/json; charset=utf-8".toMediaType())
        request.method(method, requestBody)
        client.newCall(request.build()).execute().use { response ->
            val status = response.code
            val text = response.body?.string() ?: ""
            val result = try { JSONObject(text) } catch (_: Exception) { JSONObject() }
            if (status !in 200..299) throw ApiException(status, result.optString("error", "HTTP $status"))
            return result
        }
    }
}
