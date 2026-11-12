import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

dependencies {
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
}

android {
    namespace = "com.zgotify.app"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.zgotify.app"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0-internal"
    }
    buildTypes {
        debug { applicationIdSuffix = ".internal" }
        release { isMinifyEnabled = false }
    }
    buildFeatures { buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlin { compilerOptions { jvmTarget.set(JvmTarget.JVM_17) } }
}
