import { useEffect, useRef, useState } from 'react'
import { ChatMessage } from './ChatMessage'
import { ChatInput } from './ChatInput'
import { useChat } from '../hooks/useChat'
import { fetchAuthSession } from 'aws-amplify/auth'
import { Loader2 } from 'lucide-react'

interface ChatContainerProps {
  user: any
}

export function ChatContainer({ user }: ChatContainerProps) {
  const { messages, sendMessage, isStreaming, isInitialized, initializationError, initializeConversation } = useChat()
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const hasInitialized = useRef(false)
  const [accessToken, setAccessToken] = useState<string>('')

  // Fetch access token
  useEffect(() => {
    const getToken = async () => {
      try {
        const session = await fetchAuthSession()
        const token = session.tokens?.accessToken?.toString() || ''
        setAccessToken(token)
      } catch (error) {
        console.error('Error fetching auth session:', error)
      }
    }
    getToken()
  }, [])

  // Auto-scroll to bottom when new messages arrive
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Initialize conversation on first load
  useEffect(() => {
    if (
      isInitialized &&
      !hasInitialized.current &&
      messages.length === 0 &&
      accessToken &&
      user
    ) {
      hasInitialized.current = true
      initializeConversation(
        accessToken,
        user.username
      )
    }
  }, [isInitialized, messages.length, accessToken, user, initializeConversation])

  const handleSendMessage = async (message: string) => {
    if (!accessToken || !user) return

    await sendMessage(
      message,
      accessToken,
      user.username
    )
  }

  const [pendingAuthUrl, setPendingAuthUrl] = useState<string | null>(null)
  const pendingRetryMessage = useRef<string | null>(null)

  useEffect(() => {
    if (isStreaming || messages.length === 0) return
    const last = messages[messages.length - 1]
    if (last.role !== 'assistant') return
    const match = last.content?.match(
      /https:\/\/bedrock-agentcore\.[^/]+\.amazonaws\.com\/identities\/oauth2\/authorize\?[^\s"'<)]+/
    )
    if (!match) return
    pendingRetryMessage.current = [...messages].reverse().find(m => m.role === 'user')?.content ?? null
    setPendingAuthUrl(match[0])
  }, [messages, isStreaming])

  const handleConsentClick = () => {
    if (!pendingAuthUrl) return
    window.open(pendingAuthUrl, 'gmail_consent', 'width=600,height=700,left=200,top=100')
    setPendingAuthUrl(null)
    // Poll the callback server — works regardless of whether browser opened popup or new tab
    const interval = setInterval(async () => {
      try {
        const { granted } = await fetch('http://localhost:9090/oauth2/status').then(r => r.json())
        if (!granted) return
        clearInterval(interval)
        const retryMsg = pendingRetryMessage.current
        if (retryMsg && accessToken && user)
          sendMessage(retryMsg, accessToken, user.username)
      } catch { /* callback server unreachable, keep polling */ }
    }, 2000)
  }

  if (initializationError) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <p className="text-red-400 mb-2">❌ {initializationError}</p>
          <p className="text-gray-400 text-sm">Please check your CloudFormation stack configuration</p>
        </div>
      </div>
    );
  }

  if (!isInitialized) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="w-8 h-8 text-blue-500 animate-spin mx-auto mb-2" />
          <p className="text-gray-400">Initializing agent...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 flex flex-col h-full overflow-hidden">
      {/* Scrollable message history */}
      <div className="flex-1 overflow-y-auto px-4 py-6">
        <div className="max-w-4xl mx-auto space-y-4">
          {messages.map((message, index) => {
            const isLastMessage = index === messages.length - 1
            const isStreamingMessage = isStreaming && isLastMessage && message.role === 'assistant'

            return (
              <ChatMessage
                key={`${message.timestamp}-${index}`}
                message={message}
                isStreaming={isStreamingMessage}
              />
            )
          })}

          {isStreaming && messages[messages.length - 1]?.role === 'user' && (
            <div className="flex justify-start">
              <div className="bg-[#0b2545] text-gray-200 border border-[#298dff] rounded-2xl px-4 py-3 animate-thinking-pulse">
                <div className="flex items-center gap-2">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  <span className="text-sm">💭 Host Agent is thinking...</span>
                </div>
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>
      </div>

      {/* Gmail consent button — shown when agent returns an auth URL */}
      {pendingAuthUrl && (
        <div className="flex-shrink-0 px-4 py-2 bg-[#1a1e27] border-t border-[#298dff]">
          <button
            onClick={handleConsentClick}
            className="w-full py-2 px-4 bg-[#298dff] hover:bg-blue-500 text-white rounded-lg text-sm font-medium transition-colors"
          >
            🔑 Click to authorize Gmail access — email will send automatically after
          </button>
        </div>
      )}

      {/* Fixed chat input at bottom */}
      <div className="flex-shrink-0">
        <ChatInput
          onSend={handleSendMessage}
          disabled={isStreaming}
        />
      </div>
    </div>
  )
}
